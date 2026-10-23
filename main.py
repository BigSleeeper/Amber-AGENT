from agent.agent import agent_cnt, agent_loop
from napcat_adapter.agent_api import start_adapter
from napcat_adapter.agent_api import napcat_receive

# 全局上下文
global_context = []

print("Amber-AGENT V0.0.1")

def receive_QQ_message():
    events = napcat_receive()

    # 无任何事件，返回 None
    if not events:
        return None


    text = []
    group_id = ""

    # 依次解析所有事件
    for event in events:
        raw = event["content"]

        # 1.普通消息
        if event["type"] == "message":
            # 私聊消息，后续再完善
            if raw["message_type"] == "private":
                pass

            # 群聊消息
            elif raw["message_type"] == "group":
                group_id = raw.get("group_id")          # 群号；临时私聊也可能附带来源群。
                user_id = raw.get("user_id")            # 发送者 QQ。
                raw_message = raw.get("raw_message")    # 原始消息文本，可能含 CQ 码。
                sender = raw.get("sender")
                nickname = sender.get("nickname")       # 昵称
                text.append(f"{nickname}({user_id}): {raw_message}")

        # 2.发送回执
        elif event["type"] == "message_sent":
            pass

        # 3.通知（戳一戳、撤回等）
        elif event["type"] == "notice":
            pass

        # 4.请求（好友申请、加群申请等）
        elif event["type"] == "request":
            pass
        # 5.连接与运行状态事件（心跳、生命周期）
        elif event["type"] == "meta_event":
            pass

    if text:
        content = (
            f"新消息：\n"
            f"{"\n".join(text)}"
        )
        return content

    return None

# 启动napcat适配器
start_adapter()

while True:
    agent_cnt[0] += 1

    query = receive_QQ_message()
    if not query:
        continue
    print(query)

    global_context.append({"role":"user","content":query})
    print("")
    agent_loop(global_context,query)

    response_content = global_context[-1].get("content")
    print("LCC_AGENT:[",response_content,"]\n")
    agent_cnt[1] = 0

