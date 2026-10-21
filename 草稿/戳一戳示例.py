"""戳一戳分段用法：直接发送、解析回执、从统一接收批次中识别通知。"""

import json
from napcat_adapter.agent_api import send_poke, napcat_receive

# 前提：适配器已启动并连接 NapCat；GUI 中启用目标会话、按模式放行。
# 以下发送调用是替代用法，按需选择，不要依次运行整份文件。

# 1. 群内戳成员：user_id 必填；group_id 可选，填写后表示群内动作。
result = send_poke(user_id=10001, group_id=123456)

# 2. 私聊戳好友：省略 group_id，user_id 是会话 QQ。
result = send_poke(user_id=10001)

# 3. target_id 可选：覆盖实际被戳者，省略时使用 user_id。
result = send_poke(user_id=10001, target_id=10002)
# 不填写 message_type、message、sender、time、message_id。
# 目标可用正整数/数字字符串；缺少 user_id 抛 TypeError，无效参数返回 not_sent。
# NapCat Packet 能力不足等由失败回执说明；适配器不会自动回戳。

# 4. 当前发送仍返回 JSON 字符串；“返回字典”只是建议，尚未实施。
receipt = json.loads(result)
status = receipt["status"]                    # ok / failed / not_sent / unknown。
success = receipt["success"]                  # 是否明确成功。
request_id = receipt.get("request_id")         # 请求编号，本地校验失败时可能没有。
conversation_id = receipt.get("conversation_id")
action = receipt.get("action")                # 进入发送链路后为 send_poke。
data = receipt.get("data")                    # 原生 data；成功可能为 None。
raw_response = receipt.get("raw_response")    # 完整 NapCat 回执，有网络响应时提供。
error = receipt.get("error")                  # 不保证存在，也不补造空字段。
# 成功不要求有 message_id。unknown 表示结果不确定，不自动重试。

# 5. 所有接收统一为列表；若主循环已有 events，直接遍历，不要再取另一批。
events = napcat_receive()                     # [] 或 [{"type": str, "content": dict}, ...]。
for item in events:
    event = item["content"]                  # 完整原始事件，不是 JSON 字符串。
    if (item["type"] != "notice" or event.get("notice_type") != "notify"
            or event.get("sub_type") != "poke"):
        continue                             # 当前片段仅展示戳一戳解析。

    post_type = event["post_type"]            # notice。
    notice_type = event["notice_type"]        # notify。
    sub_type = event["sub_type"]              # poke。
    timestamp = event.get("time")             # Unix 秒。
    self_id = event.get("self_id")            # 机器人 QQ。
    group_id = event.get("group_id")          # 群内有群号，私聊通常没有。
    user_id = event.get("user_id")            # 群内为操作者；私聊为会话 QQ。
    sender_id = event.get("sender_id")        # 私聊实际操作者，可选。
    target_id = event.get("target_id")        # 实际被戳者 QQ。
    raw_info = event.get("raw_info")          # 动作描述，保留原始类型。

    operator_id = user_id if group_id is not None or sender_id is None else sender_id
    poked_bot = target_id is not None and self_id is not None and str(target_id) == str(self_id)
    print(operator_id, "戳了", target_id, "是否戳机器人：", poked_bot, "群号：", group_id)
    print(json.dumps(event, ensure_ascii=False))  # 仅用于展示。
    for field, value in event.items():         # 包括未来新增的扩展字段。
        print(field, value)

# 同一接口还接收消息、其他通知、请求、自身消息、心跳及未知类型，见解析消息示例。
# 不再忽略自身动作或仅接收戳机器人的动作；GUI 启用/人工放行仍生效。
# notice 不保证有 message、message_id、sender；外层也没有旧 sender_id/metadata。
# 若需要其他事件，应在同一个主循环里处理，不能按类型多次轮询或随意丢弃。
