from agent.agent import agent_cnt, agent_loop
from napcat_adapter.agent_api import start_adapter
from napcat_adapter.agent_api import napcat_receive

# 全局上下文
global_context = []

print("Amber-AGENT V0.0.1")

def receive_QQ_message():
    events = napcat_receive()
    if not events:
        return None

    text = []
    group_id = ""
    for event in events:
        raw = event["content"]
        if event["type"] == "message":
            group_id = raw.get("group_id")          # 群号；临时私聊也可能附带来源群。
            user_id = raw.get("user_id")            # 发送者 QQ。
            raw_message = raw.get("raw_message")    # 原始消息文本，可能含 CQ 码。
            sender = raw.get("sender")
            nickname = sender.get("nickname")       # 昵称
            text.append(f"{nickname}({user_id}): {raw_message}")

    content = (
        f"群聊 {group_id} 中最近50条消息：\n"
        f"{"\n".join(text)}"
    )

    return content

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

