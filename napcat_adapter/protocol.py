"""独立于模型角色、工具定义的消息协议。所有 IPC 数据均为 JSON 对象。"""

import re


def parse_conversation(value):
    match = re.fullmatch(r"qq:(group|private):([1-9][0-9]*)", str(value))
    if not match:
        raise ValueError("conversation_id 应为 qq:group:群号 或 qq:private:QQ号")
    return match[1], int(match[2])


def normalize_event(event, *, allowed_groups=None, allowed_users=None,
                    group_only=False, require_at=False):
    """将 OneBot 消息转成通用信封；非文本段保留类型及占位符，不下载媒体。"""
    if not isinstance(event, dict) or event.get("post_type") != "message":
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
        "metadata": {
            "self_id": str(self_id),
            "sender_name": str(sender.get("card") or sender.get("nickname") or ""),
            "mentioned_bot": mentioned, "reply_to": reply_to,
            "segments": segments,
        },
    }


def send_result(command, status, *, message_id=None, error=None):
    result = {
        "type": "send_result", "request_id": command.get("request_id"),
        "conversation_id": command.get("conversation_id"),
        "success": status == "ok", "status": status,
    }
    if message_id is not None:
        result["message_id"] = str(message_id)
    if error:
        result["error"] = error
    return result
