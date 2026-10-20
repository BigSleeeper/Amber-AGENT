"""配置本机 NapCat 的正向 WebSocket，保留现有服务及其他配置。

配置结构依据 NapCat 官方 config/basic 与 OneBotConfigSchema。
仅操作用户选定或明确命中的 onebot11*.json，不扫描磁盘或修改 Agent。
"""

import copy
import json
import os
import secrets
import shutil
import socket
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


SERVICE_NAME = "lcc-agent-adapter"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""}


@dataclass(frozen=True)
class SetupResult:
    path: Path
    url: str
    token: str = field(repr=False)
    changed: bool = False
    backup: Path | None = None


def _read(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not (path.name == "onebot11.json" or
            (path.name.startswith("onebot11_") and path.suffix == ".json")):
        raise ValueError("请选择 NapCat 的 onebot11.json 或 onebot11_账号.json 文件")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("配置文件过大，无法自动配置")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("配置文件不是标准 UTF-8 JSON；请先在 NapCat 中保存为 JSON 后重试") from exc
    if not isinstance(data, dict) or not isinstance(data.get("network"), dict):
        raise ValueError("未找到 NapCat network 配置，原文件未修改")
    servers = data["network"].get("websocketServers", [])
    if not isinstance(servers, list) or any(not isinstance(item, dict) for item in servers):
        raise ValueError("websocketServers 格式异常，原文件未修改")
    return path, raw, data


def server_url(server):
    host, port = server.get("host", "127.0.0.1"), server.get("port", 3001)
    if (not isinstance(host, str) or host not in LOCAL_HOSTS or isinstance(port, bool) or
            not isinstance(port, int) or not 1 <= port <= 65535):
        return None
    # 未指定监听地址映射为可连接的回环地址。
    host = "[::1]" if host in ("::", "::1") else "127.0.0.1"
    return f"ws://{host}:{port}"


def find_configs(saved="", roots=None):
    """有限深度搜索常见目录；多账号不自动选中。"""
    if saved and Path(saved).is_file():
        return [Path(saved).resolve()]
    roots = roots if roots is not None else [
        os.getenv("NAPCAT_CONFIG_DIR", ""), Path.cwd() / "NapCat", Path.cwd() / "napcat",
        Path.cwd().parent / "NapCat", Path.home() / "Desktop" / "NapCat",
        Path.home() / "Downloads" / "NapCat", Path.home() / "NapCat",
    ]
    found = {}
    for root in roots:
        if not root:
            continue
        root = Path(root).expanduser()
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for relative in (".", "config", "napcat/config", "resources/app/napcat/config",
                             "resources/app/app_launcher/napcat/config"):
                try:
                    candidates.extend((root / relative).glob("onebot11*.json"))
                except OSError:
                    continue
        for candidate in candidates:
            if candidate.is_file():
                found[str(candidate.resolve()).casefold()] = candidate.resolve()
    # 账号专属配置优先于不会覆盖已登录账号的默认模板。
    accounts = [path for path in found.values() if path.name.startswith("onebot11_")]
    return sorted(accounts or found.values(), key=str)


def _free_port(excluded=()):
    for port in (*range(3001, 3021), 0):
        if port in excluded:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
                selected = sock.getsockname()[1]
                if selected not in excluded:
                    return selected
            except OSError:
                pass
    raise ValueError("没有可用的本机端口")


def configure_local(path):
    path, original, data = _read(path)
    updated = copy.deepcopy(data)
    servers = updated["network"].setdefault("websocketServers", [])
    used_ports = {item.get("port") for key in ("websocketServers", "httpServers", "httpSseServers")
                  for item in updated["network"].get(key, [])
                  if isinstance(item, dict) and item.get("enable") is True and isinstance(item.get("port"), int)}
    owned = [item for item in servers if item.get("name") == SERVICE_NAME]
    if len(owned) > 1:
        raise ValueError("发现重复的适配器服务名，请先在 NapCat 中处理")
    reusable = [item for item in servers if item.get("enable") is True and
                item.get("messagePostFormat") == "array" and server_url(item)]
    if owned:
        server = owned[0]
        existing_url = server_url(server)
        port = server.get("port", 3001) if existing_url and server.get("enable") is True else _free_port(used_ports)
        server.update(enable=True, host="127.0.0.1", port=port, messagePostFormat="array",
                      reportSelfMessage=False, enableForcePushEvent=True)
        if not isinstance(server.get("token"), str) or not server["token"]:
            server["token"] = secrets.token_urlsafe(32)
    elif reusable:
        server = reusable[0]
    else:
        server = dict(name=SERVICE_NAME, enable=True, host="127.0.0.1", port=_free_port(used_ports),
                      messagePostFormat="array", reportSelfMessage=False,
                      token=secrets.token_urlsafe(32), enableForcePushEvent=True,
                      debug=False, heartInterval=30000)
        servers.append(server)
    if not isinstance(server.get("token", ""), str):
        raise ValueError("WebSocket Token 格式异常，原文件未修改")
    changed = updated != data
    backup = None
    if changed:
        # 写入前检查并发编辑，备份原始字节；临时文件与目标同目录以保证原子替换。
        if path.read_bytes() != original:
            raise ValueError("NapCat 配置已被其他程序修改，请重试")
        backup = path.with_name(path.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + "." + uuid.uuid4().hex[:8] + ".bak")
        shutil.copy2(path, backup)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".lcc-napcat-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(updated, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            shutil.copymode(path, temporary)
            if path.read_bytes() != original:
                raise ValueError("NapCat 配置已被其他程序修改，请重试")
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return SetupResult(path=path, url=server_url(server), token=server.get("token", ""),
                       changed=changed, backup=backup)


def saved_token(path, url):
    """只从已选文件中读取与已保存地址相符的 Token，不在适配器内复制持久化。"""
    if not path:
        return ""
    try:
        _, _, data = _read(path)
        for server in data["network"].get("websocketServers", []):
            if (server.get("enable") is True and server_url(server) == url and
                    isinstance(server.get("token", ""), str)):
                return server.get("token", "")
    except (OSError, ValueError):
        pass
    return ""
