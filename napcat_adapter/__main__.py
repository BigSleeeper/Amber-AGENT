"""python -m napcat_adapter：stdin/stdout 为 UTF-8 JSONL，日志只写 stderr。"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading

from .client import NapCatAdapter


def start_stdin(loop, accept, stop):
    def read():
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except ValueError:
                    command = None
                loop.call_soon_threadsafe(accept, command)
        except (OSError, UnicodeError, RuntimeError):
            logging.warning("stdin 读取失败")
        finally:
            try:
                loop.call_soon_threadsafe(stop)
            except RuntimeError:
                pass
    threading.Thread(target=read, name="napcat-stdin", daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001"))
    parser.add_argument("--allow-group", type=int, action="append")
    parser.add_argument("--allow-user", type=int, action="append")
    parser.add_argument("--group-only", action="store_true")
    parser.add_argument("--require-at", action="store_true", help="兼容旧选项；统一接收不按 @ 过滤")
    parser.add_argument("--api-timeout", type=float, default=15)
    parser.add_argument("--reconnect-delay", type=float, default=3)
    parser.add_argument("--gui", action="store_true", help="显示三页管理界面")
    args = parser.parse_args()
    if args.api_timeout <= 0 or args.reconnect_delay <= 0:
        parser.error("超时和重连间隔必须大于零")
    if not args.url.startswith(("ws://", "wss://")):
        parser.error("--url 必须使用 ws:// 或 wss://")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    def emit(event):
        sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    if args.gui:
        from .panel import run
        return run(args, emit)

    adapter = NapCatAdapter(
        args.url, os.getenv("NAPCAT_TOKEN", ""),
        allowed_groups=args.allow_group, allowed_users=args.allow_user,
        group_only=args.group_only, require_at=args.require_at,
        api_timeout=args.api_timeout, reconnect_delay=args.reconnect_delay,
    )
    def ready_input(loop, accept, stop):
        start_stdin(loop, accept, stop)
        emit({"type": "ready", "protocol_version": 1})

    try:
        asyncio.run(adapter.run(emit, ready_input))
    except ModuleNotFoundError:
        print('请先安装依赖：python -m pip install -r napcat_adapter/requirements.txt', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
