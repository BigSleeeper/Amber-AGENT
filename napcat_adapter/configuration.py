"""仅包含适配器的环境设置，不依赖或修改项目 config.py。"""

import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


def _positive(value, name):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return number


def _ids(value):
    pieces = re.split(r"[\s,，;；]+", value.strip())
    if pieces == [""]:
        return None
    if any(not item.isdecimal() or int(item) <= 0 for item in pieces):
        raise ValueError("NAPCAT_ALLOW_GROUPS / NAPCAT_ALLOW_USERS 只能填写正整数")
    return tuple(sorted({int(item) for item in pieces}))


def _boolean(value, name):
    value = value.lower().strip()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} 应为 1 或 0")


@dataclass(frozen=True)
class AdapterConfig:
    url: str = "ws://127.0.0.1:3001"
    allowed_groups: tuple | None = None
    allowed_users: tuple | None = None
    group_only: bool = True
    require_at: bool = False
    api_timeout: float = 15.0
    reconnect_delay: float = 3.0
    send_timeout: float = 20.0
    startup_timeout: float = 10.0
    inbox_capacity: int = 500
    gui: bool = True

    def validate(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ("ws", "wss") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("NAPCAT_WS_URL 必须是有效的 ws/wss 地址，Token 请使用 NAPCAT_TOKEN")
        parsed.port
        for name in ("api_timeout", "reconnect_delay", "send_timeout", "startup_timeout"):
            _positive(getattr(self, name), name)
        if not isinstance(self.inbox_capacity, int) or not 1 <= self.inbox_capacity <= 100000:
            raise ValueError("NAPCAT_INBOX_CAPACITY 应为 1 至 100000 的整数")
        return self

    def arguments(self):
        args = ["--url", self.url, "--api-timeout", str(self.api_timeout),
                "--reconnect-delay", str(self.reconnect_delay)]
        for flag, values in (("--allow-group", self.allowed_groups), ("--allow-user", self.allowed_users)):
            for value in values or ():
                args.extend([flag, str(value)])
        if self.group_only:
            args.append("--group-only")
        if self.require_at:
            args.append("--require-at")
        if self.gui:
            args.append("--gui")
        return args

    @classmethod
    def from_env(cls):
        api_timeout = _positive(os.getenv("NAPCAT_API_TIMEOUT", "15"), "NAPCAT_API_TIMEOUT")
        return cls(
            url=os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001"),
            allowed_groups=_ids(os.getenv("NAPCAT_ALLOW_GROUPS", "")),
            allowed_users=_ids(os.getenv("NAPCAT_ALLOW_USERS", "")),
            group_only=_boolean(os.getenv("NAPCAT_GROUP_ONLY", "1"), "NAPCAT_GROUP_ONLY"),
            require_at=_boolean(os.getenv("NAPCAT_REQUIRE_AT", "0"), "NAPCAT_REQUIRE_AT"),
            api_timeout=api_timeout,
            reconnect_delay=_positive(os.getenv("NAPCAT_RECONNECT_DELAY", "3"), "NAPCAT_RECONNECT_DELAY"),
            send_timeout=_positive(os.getenv("NAPCAT_SEND_TIMEOUT", str(api_timeout + 5)), "NAPCAT_SEND_TIMEOUT"),
            startup_timeout=_positive(os.getenv("NAPCAT_STARTUP_TIMEOUT", "10"), "NAPCAT_STARTUP_TIMEOUT"),
            inbox_capacity=int(os.getenv("NAPCAT_INBOX_CAPACITY", "500")),
            gui=_boolean(os.getenv("NAPCAT_GUI", "1"), "NAPCAT_GUI"),
        ).validate()
