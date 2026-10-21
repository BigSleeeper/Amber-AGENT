"""独立于模型角色、工具定义的消息协议。所有 IPC 数据均为 JSON 对象。"""

from copy import deepcopy
from enum import Enum
import json
import math
import re


SYSTEM_CONVERSATION = "qq:system:0"


class NapCatReceiveType(str, Enum):
    MESSAGE = "message"
    MESSAGE_SENT = "message_sent"
    NOTICE = "notice"
    REQUEST = "request"
    META_EVENT = "meta_event"
    UNKNOWN = "unknown"


def received_item(event):
    """公共接收格式：仅包含枚举字符串 type 和完整原始字典 content。"""
    post_type = event.get("post_type")
    try:
        kind = NapCatReceiveType(post_type).value
    except (ValueError, TypeError):
        kind = NapCatReceiveType.UNKNOWN.value
    return {"type": kind, "content": deepcopy(event)}


def parse_conversation(value):
    if value == SYSTEM_CONVERSATION:
        return "system", 0
    match = re.fullmatch(r"qq:(group|private):([1-9][0-9]*)", str(value))
    if not match:
        raise ValueError("conversation_id 应为 qq:group:群号 或 qq:private:QQ号")
    return match[1], int(match[2])


def validate_send_params(params):
    """校验 NapCat 发送参数，返回会话目标和独立参数快照。"""
    if not isinstance(params, dict):
        raise ValueError("缺少 params 发送参数对象")
    kind = params.get("message_type")
    if kind not in ("group", "private"):
        raise ValueError("message_type 必须明确填写 group 或 private")
    key = "group_id" if kind == "group" else "user_id"
    target = params.get(key)
    if isinstance(target, bool) or not isinstance(target, (int, str)) or not re.fullmatch(r"[1-9][0-9]*", str(target)):
        raise ValueError(f"{key} 必须填写正整数或数字字符串")
    if kind == "group" and params.get("user_id") is not None:
        raise ValueError("群聊发送请填写 group_id，不要填写 user_id")
    if kind == "private" and params.get("group_id") is not None:
        group = params["group_id"]
        if isinstance(group, bool) or not isinstance(group, (int, str)) or not re.fullmatch(r"[1-9][0-9]*", str(group)):
            raise ValueError("临时私聊的 group_id 必须是正整数或数字字符串")
    message = params.get("message")
    if isinstance(message, str):
        if not message.strip():
            raise ValueError("message 不能为空")
    else:
        segments = [message] if isinstance(message, dict) else message
        if not isinstance(segments, list) or not segments:
            raise ValueError("message 必须填写字符串、消息段或非空消息段数组")
        for segment in segments:
            if (not isinstance(segment, dict) or not isinstance(segment.get("type"), str)
                    or not segment["type"].strip() or not isinstance(segment.get("data"), dict)):
                raise ValueError("每个消息段必须包含非空 type 和 data 对象")
    escape = params.get("auto_escape", False)
    if not isinstance(escape, bool) and escape not in ("true", "false"):
        raise ValueError("auto_escape 应为布尔值或 true/false 字符串")
    try:
        json.dumps(params, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("发送参数必须是有效 JSON 数据") from exc
    return f"qq:{kind}:{int(target)}", deepcopy(params)


def validate_send_command(command):
    if command.get("type") == "call_api":
        validate_api_call(command.get("action"), command.get("params"), command.get("timeout"))
        if command.get("stream") is not None and not isinstance(command["stream"], bool):
            raise ValueError("stream 必须为布尔值或 None")
        conversation, params = SYSTEM_CONVERSATION, deepcopy(command["params"])
    elif command.get("type") == "send_poke":
        conversation, params = validate_poke_params(command.get("params"))
    elif command.get("type") == "send_message":
        conversation, params = validate_send_params(command.get("params"))
    else:
        raise ValueError("只支持 send_message、send_poke 或 call_api 命令")
    if command.get("conversation_id") != conversation:
        raise ValueError("conversation_id 与发送参数中的目标不一致")
    return conversation, params


def validate_api_call(action, params, timeout=None):
    """仅校验传输格式；原生业务参数由当前 NapCat 版本校验，不维护 action 白名单。"""
    if not isinstance(action, str) or not action.strip() or action != action.strip():
        raise ValueError("action 必须是非空接口名称，不能包含首尾空白")
    if not isinstance(params, dict):
        raise ValueError("params 必须为 JSON 对象")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout 必须为有限的正数秒数")
    try:
        json.dumps(params, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("params 必须是有效 JSON 数据") from exc


def raw_event_envelope(event):
    """内部 GUI 信封；所有事件只生成一份，公共接口不暴露展示字段。"""
    post_type = event.get("post_type", "unknown")
    detail = event.get("notice_type") or event.get("request_type") or event.get("meta_event_type") or event.get("message_type", "")
    conversation = SYSTEM_CONVERSATION
    group = event.get("group_id")
    peer = event.get("user_id")
    if event.get("message_type") == "private":
        group = None  # 临时私聊也可能携带来源群号，不能据此归入群聊。
    elif event.get("message_type") == "group" and group is None and post_type == "message_sent":
        group = event.get("target_id")
    if post_type == "message_sent" or peer == event.get("self_id"):
        peer = event.get("target_id")
    try:
        if group is not None:
            _, target = parse_conversation(f"qq:group:{group}")
            conversation = f"qq:group:{target}"
        elif peer is not None:
            _, target = parse_conversation(f"qq:private:{peer}")
            conversation = f"qq:private:{target}"
    except ValueError:
        pass
    envelope = dict(type="event", source="qq", conversation_id=conversation,
                    text=f"[{post_type}] {detail}", raw_event=deepcopy(event))
    # 预览解析失败或被旧展示规则忽略，也不会丢失原始事件。
    try:
        preview = normalize_event(event)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        preview = None
    if preview is not None:
        envelope.update({key: value for key, value in preview.items()
                         if key not in ("type", "raw_event", "conversation_id")})
    return envelope


def validate_poke_params(params):
    """戳一戳按 NapCat 的 user_id / group_id / target_id 参数校验。"""
    if not isinstance(params, dict):
        raise ValueError("缺少 params 戳一戳参数对象")
    if set(params) - {"user_id", "group_id", "target_id"}:
        raise ValueError("戳一戳只接受 user_id、group_id 和 target_id")
    for key in ("user_id", "group_id", "target_id"):
        if key != "user_id" and key not in params:
            continue
        value = params.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, str))
                or not re.fullmatch(r"[1-9][0-9]*", str(value))):
            raise ValueError(f"{key} 必须填写正整数或数字字符串")
    kind = "group" if "group_id" in params else "private"
    peer = params["group_id"] if kind == "group" else params["user_id"]
    return f"qq:{kind}:{int(peer)}", deepcopy(params)


def command_preview(command, params):
    if command["type"] == "call_api":
        return f"[API] {command['action']} " + json.dumps(params, ensure_ascii=False)
    if command["type"] == "send_poke":
        return f"[戳一戳] 目标 QQ：{params.get('target_id', params['user_id'])}"
    return message_preview(params["message"])


def message_preview(message):
    """仅生成 GUI 预览，实际发送内容始终使用完整 message。"""
    if isinstance(message, str):
        return message
    segments = [message] if isinstance(message, dict) else message
    return "".join(str(segment["data"].get("text", "")) if segment["type"] == "text"
                   else f"[{segment['type']}]" for segment in segments)


def normalize_event(event, *, allowed_groups=None, allowed_users=None,
                    group_only=False, require_at=False):
    """生成展示信封并完整保留原始事件；非文本段不下载媒体。"""
    if not isinstance(event, dict):
        return None
    if event.get("post_type") == "notice":
        return normalize_poke_event(event, allowed_groups=allowed_groups,
                                    allowed_users=allowed_users, group_only=group_only,
                                    require_at=require_at)
    if event.get("post_type") != "message":
        return None
    kind = event.get("message_type")
    if kind not in ("group", "private") or (group_only and kind != "group"):
        return None
    user_id, self_id = int(event["user_id"]), int(event["self_id"])
    if user_id == self_id or (allowed_users is not None and user_id not in allowed_users):
        return None
    target = int(event["group_id"]) if kind == "group" else user_id
    if kind == "group" and allowed_groups is not None and target not in allowed_groups:
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        raise ValueError("请在 NapCat 中将消息格式设为 array")
    text, mentioned, reply_to = [], False, None
    labels = {"image": "图片", "record": "语音", "video": "视频",
              "file": "文件", "face": "表情", "forward": "合并转发"}
    for segment in segments:
        segment_type = segment["type"]
        data = segment.get("data", {})
        if segment_type == "text":
            text.append(str(data.get("text", "")))
        elif segment_type == "at":
            qq = str(data.get("qq", ""))
            if qq == str(self_id):
                mentioned = True
            else:
                text.append(f" @{'全体成员' if qq == 'all' else qq} ")
        elif segment_type == "reply":
            reply_to = str(data["id"])
        else:
            text.append(f"[{labels.get(segment_type, segment_type)}]")
    if kind == "group" and require_at and not mentioned:
        return None
    body = "".join(text).strip()
    if not body and not mentioned and reply_to is None:
        return None
    sender = event.get("sender") or {}
    return {
        "type": "message", "source": "qq",
        "conversation_id": f"qq:{kind}:{target}",
        "sender_id": str(user_id), "message_id": str(event["message_id"]),
        "text": body, "timestamp": int(event.get("time", 0)),
        "raw_event": deepcopy(event),
        "metadata": {
            "self_id": str(self_id),
            "sender_name": str(sender.get("card") or sender.get("nickname") or ""),
            "mentioned_bot": mentioned, "reply_to": reply_to,
            "segments": segments,
        },
    }


def normalize_poke_event(event, *, allowed_groups=None, allowed_users=None,
                         group_only=False, require_at=False):
    if event.get("notice_type") != "notify" or event.get("sub_type") != "poke":
        return None
    self_id = int(event["self_id"])
    peer = int(event["user_id"])
    # NapCat 私聊的 user_id 是会话 QQ，sender_id 才是实际操作者。
    group = event.get("group_id")
    kind = "group" if group is not None else "private"
    sender = event.get("sender_id")
    actor = int(peer if kind == "group" or sender is None else sender)
    target = int(event["target_id"])
    conversation_target = int(group) if kind == "group" else peer
    parse_conversation(f"qq:{kind}:{conversation_target}")
    if (actor == self_id or (group_only and kind == "private")
            or (allowed_users is not None and actor not in allowed_users)
            or (kind == "group" and allowed_groups is not None and conversation_target not in allowed_groups)
            or (kind == "group" and require_at and target != self_id)):
        return None
    return {
        "type": "notice", "source": "qq", "notice_type": "notify", "sub_type": "poke",
        "conversation_id": f"qq:{kind}:{conversation_target}",
        "sender_id": str(actor), "message_id": None,
        "text": f"[戳一戳] {actor} 戳了 {target}", "timestamp": int(event.get("time", 0)),
        "raw_event": deepcopy(event),
        "metadata": {
            "self_id": str(self_id), "sender_name": str(actor),
            "mentioned_bot": target == self_id, "reply_to": None, "segments": [],
            "target_id": str(target),
        },
    }


def send_result(command, status, *, message_id=None, error=None):
    result = {
        "type": "send_result", "request_id": command.get("request_id"),
        "conversation_id": command.get("conversation_id"),
        "success": status == "ok", "status": status,
    }
    if command.get("type") in ("send_poke", "call_api"):
        result["action"] = command.get("action", "send_poke")
    if message_id is not None:
        result["message_id"] = str(message_id)
    if error:
        result["error"] = error
    return result
