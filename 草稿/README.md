# 适配器收发完整用法

这里是分段阅读、按需复制的教学代码，不是独立程序。简化运行框架，保留接口、类型、必填/可选字段和回执说明；不需要整文件执行或导入，发送片段包含真实 API 调用。

- [发送消息示例.py](发送消息示例.py)：群聊/私聊、三种消息表示、消息段、普通可选参数、合并转发选项、适配器及原生回执。
- [解析消息示例.py](解析消息示例.py)：一次批量接收、六种事件类型、原始消息/发送者字段、消息段、通知/请求/心跳等解析及未知字段读取。
- [戳一戳示例.py](戳一戳示例.py)：群/私聊发送、可选目标、空成功回执、统一接收与实际操作者判断。

## 当前接口

| 操作 | 调用 | 实际返回类型 |
| --- | --- | --- |
| 接收全部事件 | napcat_receive() | list[dict]；无事件为 [] |
| 普通发送 | send_message(...) | JSON 字符串，用 json.loads() 解析 |
| 戳一戳 | send_poke(...) | JSON 字符串，用 json.loads() 解析 |

发送返回字典、统一回执固定字段等是此前的改进建议，尚未实施，示例遵从当前代码。旧 receive_message()、receive_event() 已删除。

## 启动和界面放行

主程序开始时启动一次，退出时清理；收发片段放在两者之间。此处仅展示生命周期，不包含 Agent 的主循环或工具注册。

~~~python
from napcat_adapter.agent_api import start_adapter, stop_adapter

try:
    start_adapter()
    # 放入需要的收发片段；实际轮询频率由自己的主循环控制。
finally:
    stop_adapter()
~~~

- 启动就绪不代表 NapCat/QQ 已连接，需在界面确认连接状态。
- GUI 会话默认关闭，接收和发送默认人工放行；手动模式点击“接收选中 / 发送选中”。
- 启用群/私聊可接收对应会话全部事件；启用“全部事件 / 通用 API”还可接收心跳、生命周期及其余所有会话事件。两者同时开启只交付一次。
- 心跳、自身消息和所有通知均在统一接收范围内，不再按旧仅 @ 或自身消息过滤；处理时先判断事件类型。
- napcat_receive() 第一次调用可能等待适配器启动；启动后的轮询立即取走当前已放行批次。子进程异常退出仍抛 AdapterProcessError，不能用空列表掩盖故障。

## 接收结构与完整类型

~~~python
from napcat_adapter import napcat_receive

events = napcat_receive()
for item in events:
    event_type = item["type"]  # str
    raw = item["content"]     # dict：完整 NapCat 原始事件
    print(event_type, raw)
~~~

| 外层 type | content 内的内容和细分字段 |
| --- | --- |
| message | 收到的群/私聊消息；message_type、message |
| message_sent | 自身账号发出的消息上报；不是发送调用回执 |
| notice | 所有通知；notice_type、sub_type，例如 notify/poke |
| request | 好友/群请求；request_type、sub_type、flag |
| meta_event | 心跳、生命周期；meta_event_type |
| unknown | 未识别的扩展；原始 post_type 仍在 content 内保留 |

可导入 NapCatReceiveType 枚举；实际返回的 type 始终是普通字符串。消息段 type 是另一层类型，如 text、image、at，不能和事件 type 混为一谈。

每次轮询只调用一次，随后遍历整批；不要为普通消息、戳一戳、请求分别调用，否则会取走不同批次。若不同模块都需要同一批事件，由主循环分发同一个 events。没有第二份独立接收队列，接口不重复交付已取走的项目；NapCat 自身重复上报则逐帧保留。

content 是字典，不是 JSON 字符串，也不是模型文本；旧外层 raw_event、metadata、sender_id、conversation_id 等已移除。原始数据中缺失的字段不会补造，字段名和嵌套 JSON 类型保持原样。普通消息的完整正文在 content["message"]，可以是消息段数组或 CQ 字符串，解析示例分别处理。图片/语音等原始段会保留，但接收函数不自动下载或识别媒体。

原始字段表只解释已知含义，不能代替完整数据。可选字段用 .get()，区分缺失和 JSON null 用 "字段名" in raw，不用 or 默认值误覆盖 0、False、空字符串。遍历 raw.items() 可以读取未来新增字段；只有展示/保存或确需模型文本时才 json.dumps(raw, ensure_ascii=False)。适配器不自动添加模型角色或会话历史。

## 普通发送参数

| 参数 | 是否必填 | 用途 |
| --- | --- | --- |
| message_type | 本适配器必填 | group / private，必须明确类型 |
| group_id | 群聊必填 | 目标群；临时私聊可按 NapCat 需要额外提供 |
| user_id | 私聊必填 | 目标 QQ；群聊不要额外填写 |
| message | 必填 | 非空字符串、单消息段或非空消息段数组 |
| auto_escape | 可选 | 默认 false；控制字符串中的 CQ 码是否按纯文本处理 |
| timeout | 可选 | NapCat 侧发送超时，毫秒，区别于适配器回执超时秒数 |
| source | 合并转发可选 | 来源 |
| news | 合并转发可选 | 预览列表，如 [{"text": "预览"}] |
| summary | 合并转发可选 | 卡片摘要 |
| prompt | 合并转发可选 | 会话列表提示文字 |

目标号码支持正整数或数字字符串。推荐消息段数组，按顺序组合正文；仍兼容字符串和单段。合并转发 node 与普通段不要混发。可选字段按需要填写，不是每次填满；普通发送不需要接收事件的 sender、time、message_id。路径一般属于 NapCat 所在机器，示例中的号码、路径和旧消息编号需替换。

## 发送回执

~~~python
import json

receipt = json.loads(result)  # result 来自某一次 send_message / send_poke
status = receipt["status"]
message_id = receipt.get("message_id")
error = receipt.get("error")
raw_response = receipt.get("raw_response")
~~~

- ok：明确成功；failed：明确失败；not_sent：没有发送；unknown：结果不确定，不自动重发。
- success、status 是当前发送回执的公共字段；其他字段不保证都存在，不能按此前的“固定字段”建议假定已经实现。
- 有网络响应时 raw_response 保留原生 status、retcode、data、echo、wording 等及未知扩展。原生 data 可以为 null、false、0 或其他结构，不要强制当字典。
- 本地参数无效返回 not_sent；缺少 Python 必填关键字参数直接抛 TypeError，此时没有回执可供 json.loads()。
- request_id 是适配器请求编号，echo 是网络请求匹配标识，message_id 是消息编号，三者不同。
- 戳一戳成功不要求有 message_id，原生 data 也可能为 null。
- API 操作回执通过该次调用返回，不混入 napcat_receive()；message_sent 是另一类原始事件。

## 原始定义

原始字段随 NapCat 版本和事件种类变化，以实际 content 为准：

- [事件类型与通知字段](https://doc.napneko.icu/onebot/event)
- [消息与消息段源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/types/message.ts)
- [普通发送实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/SendMsg.ts)
- [戳一戳发送](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/packet/SendPoke.ts)、[通知字段](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/event/notice/OB11PokeEvent.ts)

这些片段不修改 Agent，也不会自动回复、回戳或同意请求。实际事件消费策略由调用方维护。
