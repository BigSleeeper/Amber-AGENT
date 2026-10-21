"""分段教学：批量接收、全部事件分支、原始字段读取；不需要整文件运行。"""

import json
from napcat_adapter import napcat_receive, NapCatReceiveType

# 前提：适配器已启动、连接 NapCat，GUI 中启用会话并按模式放行。
# 启用“全部事件 / 通用 API”可接收心跳、生命周期及所有其他会话的事件。
# 每次轮询只调用一次，再遍历整批；不要为每种类型重复调用以免取到不同批次。
events = napcat_receive()  # list[dict]；立即取走当前已放行事件，无事件为 []。
for event in events:
    event_type = event["type"]  # str；下方枚举 message 等六种类型。
    raw = event["content"]      # dict，完整原始事件，不是模型文本或 JSON 字符串。
    # 外层只有 type、content，没有旧 source、raw_event、metadata 等字段。
    # 不要 json.loads(events) 或 json.loads(raw)，它们已经是 Python 对象。

    # 1. 所有事件的常见字段：缺失时 get 返回 None，不补造或转换原始值。
    post_type = raw.get("post_type")  # 原始类型；未知类型仍保留原 post_type。
    timestamp = raw.get("time")      # Unix 秒，是否提供取决于实际事件。
    self_id = raw.get("self_id")      # 机器人自身 QQ 号。

    # 2. 收到消息：完整已知消息字段。部分为可选扩展，不保证每条都有。
    if event_type == NapCatReceiveType.MESSAGE.value:  # 也可直接比较 "message"。
        message_type = raw.get("message_type")  # group / private。
        sub_type = raw.get("sub_type")          # normal、friend、group 等。
        user_id = raw.get("user_id")            # 发送者 QQ。
        message_id = raw.get("message_id")      # 消息编号，可用于引用/撤回。
        message_seq = raw.get("message_seq")    # 消息序列扩展。
        real_id = raw.get("real_id")            # 内部 ID 扩展。
        real_seq = raw.get("real_seq")          # 真实序列号，可能为字符串。
        group_id = raw.get("group_id")          # 群号；临时私聊也可能附带来源群。
        group_name = raw.get("group_name")      # 群名，可选。
        target_id = raw.get("target_id")        # 目标编号，可选。
        temp_source = raw.get("temp_source")    # 临时会话来源，可选。
        message_sent_type = raw.get("message_sent_type")  # 发送类型扩展。
        message = raw.get("message")            # 消息段数组或 CQ 字符串。
        message_format = raw.get("message_format")  # array / string，可选。
        raw_message = raw.get("raw_message")    # 原始消息文本，可能含 CQ 码。
        font = raw.get("font")                  # 字体编号，可选。
        original_raw = raw.get("raw")           # NapCat 内部原始对象扩展。
        emoji_likes_list = raw.get("emoji_likes_list")  # 表情回应列表，可选。
        anonymous = raw.get("anonymous")        # 匿名信息，可缺失或为 None。

        # sender 中的全部已知字段；是否存在取决于群/私聊及实际版本。
        sender = raw.get("sender")
        if isinstance(sender, dict):
            sender_qq = sender.get("user_id")
            nickname = sender.get("nickname")   # QQ 昵称。
            card = sender.get("card")           # 群名片，可能为空字符串。
            role = sender.get("role")           # owner / admin / member。
            sex = sender.get("sex")             # male / female / unknown。
            age = sender.get("age")
            area = sender.get("area")
            level = sender.get("level")
            title = sender.get("title")         # 群专属头衔。

        # 消息段按顺序组合，例如 @ + 文本 + 图片。推荐 NapCat 配置 array。
        if isinstance(message, list):
            for segment in message:
                kind = segment["type"]
                data = segment["data"]
                if kind == "text":
                    print("文本：", data.get("text"))
                elif kind == "at":
                    print("@ 的 QQ：", data.get("qq"))  # all 表示全体成员。
                elif kind == "reply":
                    print("引用编号：", data.get("id"), "序列号：", data.get("seq"))
                elif kind in ("image", "record", "video", "file", "onlinefile", "flashtransfer"):
                    print("媒体/文件：", kind, data)  # file、url、file_size 等均保留。
                elif kind in ("face", "mface", "dice", "rps"):
                    print("表情/互动：", kind, data)
                elif kind in ("forward", "node"):
                    print("合并转发：", kind, data)  # 不自动查询其中的历史记录。
                elif kind in ("json", "xml", "markdown", "miniapp", "music", "contact", "location"):
                    print("卡片/富文本：", kind, data)
                else:
                    print("其他消息段：", kind, data)  # 未知段也不丢弃。
        elif isinstance(message, str):
            print("原始 CQ 字符串：", message)  # 不把字符串逐字当消息段遍历。

        # 临时私聊即使带 group_id 也应按 message_type=private 处理。
        if message_type == "group":
            print("群聊：", group_id, "发送者：", user_id)
        elif message_type == "private":
            print("私聊：", user_id)

    # 3. 自身账号发出的消息上报；不是 send_message 的调用回执。
    elif event_type == "message_sent":
        message_type = raw.get("message_type")  # group / private。
        target_id = raw.get("target_id")        # 目标 QQ / 群号，按实际字段读取。
        group_id = raw.get("group_id")
        message_id = raw.get("message_id")
        message = raw.get("message")            # 可沿用上面的字段/消息段读取方式。
        print("自身发送上报：", message_type, target_id, group_id, message_id, message)
        # 不应因为收到自身消息上报就自动回复自身消息。

    # 4. 全部通知，再按 notice_type 和 sub_type 区分。
    elif event_type == "notice":
        notice_type = raw.get("notice_type")
        sub_type = raw.get("sub_type")
        group_id = raw.get("group_id")
        user_id = raw.get("user_id")
        operator_id = raw.get("operator_id")
        if notice_type == "notify" and sub_type == "poke":
            sender_id = raw.get("sender_id")
            actor = user_id if group_id is not None or sender_id is None else sender_id
            print("戳一戳：", actor, "→", raw.get("target_id"))
        elif notice_type in ("group_recall", "friend_recall"):
            print("撤回：", raw.get("message_id"), "操作者：", operator_id)
        elif notice_type in ("group_increase", "group_decrease", "group_admin", "group_ban"):
            print("群成员变动：", notice_type, sub_type, group_id, user_id, operator_id,
                  "禁言秒数：", raw.get("duration"))
        elif notice_type == "group_upload":
            print("群文件上传：", raw.get("file"))
        elif notice_type == "group_card":
            print("群名片：", raw.get("card_old"), "→", raw.get("card_new"))
        elif notice_type == "group_msg_emoji_like":
            print("表情回应：", raw.get("message_id"), raw.get("likes"), raw.get("is_add"))
        else:
            # 好友添加、精华、群名/头衔、灰条、资料点赞、输入状态、离线、
            # 在线文件等通知，继续根据原始 notice_type/sub_type 读取具体字段。
            print("其他通知：", notice_type, sub_type, raw)

    # 5. 请求事件：接收并不表示已同意请求；这里只读取，不执行管理操作。
    elif event_type == "request":
        request_type = raw.get("request_type")  # friend / group。
        sub_type = raw.get("sub_type")          # 群请求细分 add / invite 等。
        user_id = raw.get("user_id")            # 请求者 QQ。
        group_id = raw.get("group_id")
        comment = raw.get("comment")            # 验证信息。
        flag = raw.get("flag")                  # 后续处理请求需保留的原始标识。
        print("请求：", request_type, sub_type, user_id, group_id, comment, flag)

    # 6. 协议状态事件：通常由程序内部处理，不必当作聊天文本交给模型。
    elif event_type == "meta_event":
        meta_event_type = raw.get("meta_event_type")  # heartbeat / lifecycle。
        if meta_event_type == "heartbeat":
            status = raw.get("status")          # 状态对象，如 online、good。
            interval = raw.get("interval")      # 心跳间隔，毫秒。
            print("心跳：", status, interval)
        elif meta_event_type == "lifecycle":
            print("生命周期：", raw.get("sub_type"))  # connect / enable / disable。
        else:
            print("其他协议状态：", raw)

    # 7. 未知扩展：外层为 unknown，原始 post_type 和字段仍完整保留。
    elif event_type == "unknown":
        print("未知事件：", raw.get("post_type"), raw)

    # 8. 已知字段表不能替代实际数据；遍历可以读到所有未来扩展字段。
    for field, value in raw.items():
        print(field, value)
    print(json.dumps(raw, ensure_ascii=False, indent=2))  # 仅用于展示/保存 JSON。

# 可选：确需模型文本时，在 message 分支按需转换，不要直接传 content 字典。
# model_message = {"role": "user", "content": json.dumps(raw, ensure_ascii=False)}
# 适配器不自动创建 role、提示词或会话历史。
# 缺失与 null 可用 "字段名" in raw 区分；不要用 or 默认值误覆盖 0/False。
