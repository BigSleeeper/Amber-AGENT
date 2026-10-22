"""用法片段：按需复制某一段，不要将整份文件作为程序执行。"""

import json
from napcat_adapter.agent_api import send_message

# 前提：主程序已经调用 start_adapter()，并连接 NapCat、启用目标聊天。
# 目标必须明确填写，不再根据最近收到的消息自动确定发送对象。
# 当前接口仍叫 send_message，仍返回 JSON 字符串；此前返回字典的建议尚未实施。

# 1. 群聊发送：message_type、group_id、message 为适配器要求的必要参数。
result = send_message(
    message_type="group",  # group 表示群聊。
    group_id=123456,        # 目标群号，支持整数或数字字符串。
    message=[{"type": "text", "data": {"text": "你好"}}],
)

# 2. 私聊发送：将 group_id 换成 user_id。
result = send_message(
    message_type="private",
    user_id=10001,         # 目标 QQ 号，不是机器人的 QQ 号。
    message="你好",       # message 也可以直接传字符串。
    auto_escape=True,      # 字符串按纯文本发送，不解析其中的 CQ 码。
)
# 群临时私聊可按 NapCat 需要额外提供 group_id，但不是所有私聊都必填。

# 3. message 的三种写法。以下是三种替代用法，不是要同时发送三次。
message = "你好"  # 字符串；auto_escape=False 时其中的 CQ 码会被解析。
message = {"type": "text", "data": {"text": "你好"}}  # 单个消息段。
message = [  # 消息段数组：按数组顺序组合成一条消息。
    {"type": "at", "data": {"qq": "10001"}},
    {"type": "text", "data": {"text": " 请查看消息"}},
]

# 常见消息段：按需放入 message 数组，路径、号码、消息编号均需替换。
# 文本：  {"type": "text",  "data": {"text": "你好"}}
# @用户： {"type": "at",    "data": {"qq": "10001"}}
# @全体： {"type": "at",    "data": {"qq": "all"}}
# 引用：  {"type": "reply", "data": {"id": "888"}}
# 图片：  {"type": "image", "data": {"file": "file:///C:/素材/图片.png"}}
# 语音：  {"type": "record","data": {"file": "file:///C:/素材/语音.wav"}}
# 视频：  {"type": "video", "data": {"file": "file:///C:/素材/视频.mp4"}}
# 文件：  {"type": "file",  "data": {"file": "file:///C:/素材/文件.txt", "name": "文件.txt"}}
# 表情：  {"type": "face",  "data": {"id": "14"}}
# 引用的 id 是已有消息编号；发送后自己的新消息编号由 NapCat 返回。
# 媒体的其他可选字段随消息段类型变化，见 README 中的官方类型定义。

# 4. 普通发送的可选参数（此处填全以说明用法，不表示每次都必填）。
result = send_message(
    message_type="group",
    group_id=123456,
    message="[CQ:at,qq=10001] 请查看消息",
    auto_escape=False,  # 默认 false；仅对字符串有效，消息段数组不受它影响。
    timeout=10000,      # NapCat 侧发送超时，单位毫秒；不是适配器回执超时秒数。
)

# 5. 合并转发专用可选字段。只用于 node 消息，不能把 node 与普通段混发。
# 普通发送不用填写 source、news、summary、prompt。
result = send_message(
    message_type="group",
    group_id=123456,
    message=[{
        "type": "node",
        "data": {
            "user_id": "10001",       # 这一条转发节点展示的发送者。
            "nickname": "示例发送者",  # 转发节点展示的昵称。
            "content": [{"type": "text", "data": {"text": "节点正文"}}],
        },
    }],
    source="聊天记录",                    # 合并转发的来源。
    news=[{"text": "示例发送者：节点正文"}],  # 卡片预览列表。
    summary="查看聊天记录",                # 卡片摘要。
    prompt="[聊天记录]",                   # 会话列表中的提示文字。
)
# 上述卡片扩展的实际显示效果取决于 NapCat 版本和发送实现。

# 6. 解析任意一次调用的返回值。result 是 JSON 字符串，不是字典。
receipt = json.loads(result)
success = receipt["success"]              # 布尔值；手动模式表示入队成功，自动模式表示真实发送成功。
status = receipt["status"]                # ok / failed / not_sent / unknown。
queued = receipt.get("queued", False)     # True：普通消息已入人工队列，尚未确认发送。
mode = receipt.get("mode")               # 手动入队成功时为 manual；其他结果可能没有。
detail = receipt.get("detail")            # 手动入队成功时为“已进入队列，等待人工发送”。
request_id = receipt.get("request_id")     # 适配器自动生成的请求编号。
conversation_id = receipt.get("conversation_id")  # 目标会话标识。
message_id = receipt.get("message_id")     # 成功回执可能携带的新消息编号。
error = receipt.get("error")              # 失败或结果未知时的原因。
adapter_type = receipt.get("type")         # 进入发送流程时为 send_result。
action = receipt.get("action")            # 普通发送未必提供；不要强行读取。
data = receipt.get("data")                # NapCat 原始 data，保持 null/False/0 等原值。
raw_response = receipt.get("raw_response")  # 完整 NapCat 原始回执，收到网络响应时提供。

# 7. 原生回执与适配器回执分开读取；这里不改写原生字段或假造缺失值。
if isinstance(raw_response, dict):
    napcat_status = raw_response.get("status")    # NapCat 原始状态，如 ok / failed。
    retcode = raw_response.get("retcode")         # NapCat 返回码；应结合原始 status 判断。
    napcat_data = raw_response.get("data")        # 查询/发送结果，不保证总是字典。
    wording = raw_response.get("wording")         # 原生错误说明，可选。
    napcat_message = raw_response.get("message")  # 原生说明，可选。
    echo = raw_response.get("echo")               # 网络请求匹配标识，不是消息编号。
    stream = raw_response.get("stream")           # normal-action 等协议标记，可选。
    for field, value in raw_response.items():     # 原生未知扩展也完整保留。
        print(field, value)
# 进入实际发送流程的回执还包含 type="send_result"。
# 本地参数校验失败的回执可能只有 success、status、error，故可选字段用 get。
# 缺少 Python 必填关键字参数会直接抛 TypeError，此时没有 JSON 回执可供解析。
# ok：手动模式入队成功，或自动模式真实发送成功；failed：NapCat 明确失败。
# not_sent：没有发送；unknown：结果未知，不自动重发。
# 手动普通发送入队后立即返回，实际发送仍需界面人工放行，结果在 GUI 查看。
# 这里只解析示例中某次 result；有多次实际发送时，应分别保存和处理各自回执。
