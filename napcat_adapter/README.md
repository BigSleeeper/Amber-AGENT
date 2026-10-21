# NapCat Python 适配器

由主程序启动的 Python / Tk 图形程序。通过左侧按钮切换主界面、聊天配置、NapCat 配置三个页面。Agent 与适配器使用匿名管道（stdin/stdout、UTF-8 JSONL），适配器与 NapCat 使用正向 WebSocket。无需本机 HTTP 服务、EXE 或消息网络轮询。

```text
QQ ⇄ NapCat ⇄ WebSocket ⇄ Python 适配器子进程（Tk 界面）
                                ⇅ 匿名管道 / JSONL
                          main.py ⇄ Agent / 发送工具
```

## 本次范围与完整能力入口

对外接收统一为 `napcat_receive()`：立即返回当前已放行事件的字典列表，无事件返回 `[]`。普通发送继续使用 `send_message(...)`。不修改 Agent 主循环、工具注册或目录结构；其他 NapCat 功能由适配器自行提供。

适配器通过原生 `action + params` 开放当前 NapCat OneBot WebSocket 服务提供的全部 API，不按固定接口名单裁剪；未知 action、必填参数和账号权限由所连接的 NapCat 校验。群管理、好友/加群请求处理、消息查询/撤回/转发、文件、账号资料、扩展接口均走同一通道，完整参数与原始回执保留。这里的覆盖范围是 **NapCat 对 OneBot 暴露的功能**；NapCat 安装、QQ 登录及 WebUI 管理不是 OneBot action。

在“聊天配置”启用独立的 **全部事件 / 通用 API**（`qq:system:0`）后：

- “NapCat 配置 → 打开通用 API 控制台”输入原生 action 和 JSON params，加入发送队列；手动模式到主界面选择“完整接口”并放行。支持接口超时与单帧/多帧响应选择，详情显示完整原始回执。
- 控制台可以选择 JSONL 结果文件，独占创建以避免覆盖已有文件；逐行记录中间响应和最终结果。预览只显示前 100000 个字符，大文件应选择文件保存全部帧。窗口关闭不撤销已排队/已执行请求，可在主界面处理待办。
- 所有类型都进入唯一接收通道：`message`、`message_sent`、全部 `notice`、`request`、`meta_event` 以及未知扩展事件。字段和 JSON 类型完整保留，不按消息编号去重，也不要求消息段为 array。

完整接口是独立的全局开关，启用后可接收所有原始事件并调用原生 API。已启用群/私聊的事件优先显示在对应会话，未启用会话及无会话事件显示在完整接口；两者都开启时只交付一次。只启用单个群/私聊也可接收该会话的全部事件。所有会话默认关闭，人工/自动模式为共享设置。统一接收不再按仅 @、自身消息或旧普通消息允许列表过滤，心跳和自身消息也在返回范围内。

旧公共 `receive_message()`、`receive_event()` 已替换为 `napcat_receive()`，没有两个独立接收队列。通用 API 由底层 `AdapterProcess.call_api(action, params, timeout=..., on_frame=..., stream=...)` 调用，返回字典，供适配器扩展/调试使用，不构成 Agent 接入要求。

| 通用调用选项 / 结果 | 含义 |
| --- | --- |
| `params` | 原生 JSON 对象，默认 `{}`；业务必填字段按当前 NapCat 文档填写，不补造接收事件字段 |
| `timeout` | 放行后一次网络操作的总时限，单位秒；省略使用配置值，与 `params.timeout` 的原生含义独立 |
| `stream=None` | 自动处理下载多帧；`upload_file_stream` 每次请求视为单帧响应 |
| `stream=False / True` | 分别指定单帧响应 / 允许中间 stream、reset 帧并等待终帧，供版本差异或新增接口使用 |
| `on_frame(response)` | 在调用线程逐帧接收完整中间响应；终帧在最终 `raw_response`。回调应及时返回，不在回调内同步等待另一个 API |
| `raw_response` / `data` | 完整原生终帧 / 其 data，保留 `null`、`false`、`0`、空数组、未知字段和失败码 |
| `frames` | 没有回调时收集的中间响应，最多 64 MiB；大文件使用回调或控制台 JSONL 保存 |
| `stream_complete` / `stream_error` | API 成功且中间帧处理完整才为 true；回调异常、收集超限或待处理帧超过 128 条会明确报告不完整，原生业务回执仍保留 |

上传分块和下载流的响应方式不同，已依据 [NapCat WebSocket 分发源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/network/websocket-server.ts)、[上传流实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/UploadFileStream.ts) 和 [下载流实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/DownloadFileStream.ts) 分别处理。接口名称与参数查 [NapCat API 文档](https://napcat.apifox.cn/)。WebSocket 单帧上限与官方服务端一致为 50 MiB；文件路径通常属于 NapCat 所在机器，跨机器传文件使用原生分块 API。

“全功能通道可达”与“所有业务逐项实测”分别记录：目前通过本地模拟 NapCat、真实子进程管道和 GUI 验证，尚未使用真实 QQ 逐项验证 API。账号权限、NapCat/QQ 版本、Packet 能力依赖与上游接口异常仍以实际原生回执为准。

## 启动

需要 Python 3.11 或更新版本和 Tk 支持（Windows 官方 Python 通常自带）。主程序和适配器使用同一个解释器。

```powershell
python -m pip install -r napcat_adapter/requirements.txt
python main.py
```

在 NapCat 中启用 **OneBot WebSocket 服务端**，消息格式选择 `array`。启动后到“NapCat 配置”填写地址、Token 并连接。已保存的地址在下次启动时自动连接。主程序收到就绪通知不代表 QQ 已连接，可在界面查看连接状态。

首次启动：**全部群聊和私聊关闭，接收与发送分别默认为手动模式**。在“聊天配置”启用指定会话后才能收发。后续启动恢复保存的会话和模式选择。

## 主界面

主界面左侧是已启用聊天列表，可筛选全部 / 群聊 / 私聊。只有在“聊天配置”中启用的会话才会出现在这里，关闭后立即移除。每个聊天显示待接收 / 待发送数量。

选中聊天后，右侧的接收列表和发送列表仅展示该聊天的消息，顶部待接收 / 待发送统计也仅针对当前聊天；已启用会话数为全局统计。切换聊天不会清空其他聊天的消息，新消息到达也不会自动切走当前聊天。消息详情显示完整的会话号码。

接收、发送列表分别有手动 / 自动选项，支持多选操作。**模式仍是全局设置，作用于所有启用聊天**；当前选择只是查看筛选，不会暂停其他聊天的自动收发。“全选 / 全不选、接收选中 / 发送选中、删除选中”仅操作当前聊天显示的消息。下方显示选中消息的完整内容及发送错误。

界面采用接近 Codex 的中性灰阶、扁平侧栏、细分隔线和紧凑标题。左侧底部“外观”提供“浅色 / 深色”切换，三个页面、输入框、下拉列表、消息详情及运行日志同步更新；支持时同时调整 Windows 原生标题栏。切换不重启进程、不改变连接、模式、消息队列或选中项，选择自动保存到适配器配置，下次启动恢复；旧配置默认浅色。

两个消息列表均提供“全选 / 全不选”，支持 `Ctrl+A` 全选、`Esc` 取消选择。选择本身不会接收、发送或删除消息，仍需点击对应操作按钮。

| 操作 | 接收列表 | 发送列表 |
| --- | --- | --- |
| 手动模式 | 消息等待人工处理 | Agent 请求等待人工处理，发送工具也会等待 |
| 接收 / 发送选中 | 将选中待办交给 Agent | 将选中待办交给 NapCat |
| 自动模式 | 启用会话的消息直接交给 Agent | 启用会话的发送请求直接交给 NapCat |
| 删除选中 | 待办不会交给 Agent | 待办返回 `not_sent`，解除工具等待 |

切换为自动模式会立即放行已有待办。已交给 Agent 的消息无法撤回；发送中的消息不能删除取消；删除已完成记录仅清理界面历史。列表各保留最多 500 条，优先淘汰已完成记录，待办占满时拒绝新增消息；接收丢弃数显示在顶部。

人工排队时暂停发送超时，真正开始发送后才计算 NapCat 接口超时。断线或超时返回 `unknown`，不自动重发。当前 Agent 串行运行，因此等待人工发送 / 删除时，该次 Agent 调用也会等待。

## 聊天配置

- 连接成功后获取群列表和好友列表，也可手动刷新。新发现的会话始终默认关闭。
- 支持名称 / 号码搜索、群聊 / 私聊筛选、批量开启 / 关闭、双击切换。
- “全选 / 全不选”仅作用于当前筛选后显示的会话，不直接改变启用状态；选中后再点“启用选中 / 关闭选中”。
- 尚未出现在目录中的会话可手动填写号码添加，添加后仍默认关闭。
- 关闭会话会移除尚未放行的接收消息，并取消待发送消息；已经交给主程序或已开始发送的消息不能撤回。
- GUI 模式以本页选择为准，可以同时开启群聊和私聊。旧 `NAPCAT_ALLOW_GROUPS` / `NAPCAT_ALLOW_USERS` 仅预填关闭的会话，不自动启用，也不作为群成员过滤器；`NAPCAT_GROUP_ONLY` 不限制本页私聊选择。

## NapCat 配置

支持 WebSocket 地址、Token、接口超时、重连间隔，以及保存、连接、断开和运行日志。统一接收不再提供“仅接收 @机器人”过滤，以保证全部事件可交付；按聊天配置启用和放行。修改连接参数后，先断开、等待停止，再重新连接。

### 一键自动配置

1. 点击“一键自动配置”。程序优先使用已记住的 NapCat 配置路径，其次查找 `NAPCAT_CONFIG_DIR` 和附近常见 NapCat 目录。未找到或存在多个账号时弹出文件选择框，请选择正在使用的 `onebot11_账号.json`；尚未生成账号配置时可选择 `onebot11.json` 默认模板。
2. 优先复用已开启、消息格式为 `array`、`reportSelfMessage=true` 且未关闭强制事件推送的本机正向 WebSocket 服务，保证统一接收也能获得自身消息上报。若现有其他应用服务不满足条件，保留它并新增名为 `lcc-agent-adapter` 的专用服务，监听 `127.0.0.1`，使用可用端口和随机 Token。对既有同名专用服务会升级上报选项，保留有效 Token 和端口。已有 `localhost` 服务保持该主机名，不强制改成 IPv4，以兼容仅监听 IPv6 的情况。
3. 修改前在原文件旁创建带时间与随机后缀的 `.bak` 备份；写入前检测并发修改，采用同目录临时文件原子替换。损坏的 JSON 或不符合 NapCat 结构的文件不会被覆盖。备份含原始 Token，应与原配置一样保管；需要恢复时可用备份替换原文件。
4. 自动填写适配器的地址与 Token，保存配置文件路径并连接。**新增或修改服务后，需在 NapCat WebUI 的网络配置页面重新读取并保存配置，或手动重启 NapCat，使运行中的服务加载变更**；适配器持续重连等待服务生效。不会自动重启 QQ / NapCat，也不会安装或登录 NapCat。

账号专属配置优先于默认模板，多账号时不猜测账号。`onebot11.json` 是 NapCat 默认模板：同目录已有账号文件时，自动发现优先列出账号文件；若仍手动选择默认模板，会提示选择正在使用的账号配置并停止写入，避免提示成功但配置不生效。仅在没有账号配置时允许使用默认模板。只支持标准 UTF-8 JSON；含 JSON5 注释的手写文件需先在 NapCat 中保存为 JSON。配置格式依据 [NapCat 官方配置指南](https://doc.napneko.icu/config/basic) 和 [官方配置加载规则](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/helper/config-base.ts)。

如果以前已一键配置过，升级代码后重新点击一次可修复旧专用服务关闭自身消息上报的问题；文件有改动时会备份并提示加载配置。配置写入成功不等于 NapCat 已启动或连接成功，请查看连接状态；一键配置也不会自动启用群聊/私聊或放行事件。

文件中的服务已经开启，但连接仍被拒绝且对应端口没有监听时，应检查运行配置是否已加载。仅重复点击一键配置不会重载 NapCat。WebUI 保存配置会通过官方 `OB11Config/SetConfig` 接口加载到运行中，无需重启 QQ；先重新读取配置，避免把旧页面中的配置覆盖回文件。该机制见 [官方配置保存接口](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-webui-backend/src/api/OB11Config.ts)。

适配器配置保存在本目录 `.config/panel.json`（Git 忽略），也可通过 `NAPCAT_PANEL_CONFIG` 指定路径。**Token 不复制写入适配器配置文件**。自动配置生成的 Token 保存在 NapCat 自己的服务配置中；适配器下次启动从已选择的文件读取与已保存地址匹配的 Token 到内存，环境变量 `NAPCAT_TOKEN` 非空时优先使用。手动输入且不在 NapCat 文件中的 Token 仍只在本次进程内保存。已保存的界面连接设置优先于环境提供的初始地址及超时。

## Agent 接口和生命周期

`napcat_receive() -> list[dict]` 不带参数，每次原子取走调用时已排入父进程队列的全部事件，按放行进入管道的顺序返回。随后新到事件留到下次调用；已经返回的事件不会再次返回。没有事件返回 `[]`；首次调用可能启动适配器并等待启动完成，启动后轮询本地队列不等待网络。子进程异常退出仍抛 `AdapterProcessError`，不以空列表掩盖故障。

每个字典严格只有 `type` 和 `content`：`type` 为普通字符串；`content` 为完整 NapCat 原始事件字典，保留全部字段、嵌套对象及 JSON 类型，不补造缺失字段。GUI 的会话编号、预览和元数据仅供界面使用，不混入返回结构；也不再自动生成模型文本。直接导入可用 `from napcat_adapter import napcat_receive, NapCatReceiveType`。

| `type` 枚举值 | `content` 内容 |
| --- | --- |
| `message` | 收到的群聊或私聊消息，正文在 `message` |
| `message_sent` | 自身账号发送的消息上报 |
| `notice` | 全部通知，通过 `notice_type`、`sub_type` 区分戳一戳、撤回等 |
| `request` | 好友/群请求，通过 `request_type`、`sub_type` 区分 |
| `meta_event` | 心跳、生命周期，通过 `meta_event_type` 区分 |
| `unknown` | 无法识别的扩展类型；原始 `post_type` 和所有字段仍在 `content` |

枚举定义为 `NapCatReceiveType`，实际返回值始终是字符串。API 成功/失败回执和下载中间帧仍由相应发送/API 调用返回，不伪装成接收事件。旧接收接口及依赖外层 `raw_event`、模型文本 `content` 的代码需要按本格式迁移；根目录 `草稿/` 已更新为当前接口的完整分段用法，按需阅读和复制，不整文件执行。

```python
from napcat_adapter.agent_api import napcat_receive

# 已启动并按 GUI 策略放行后，每次轮询调用：
events = napcat_receive()  # list[dict]；空队列为 []
for event in events:
    event_type = event["type"]       # str，例如 message / notice / meta_event
    content = event["content"]      # dict，完整原始事件
    if event_type == "message":
        print(content["message_type"], content["message"])
    elif event_type == "notice":
        print(content.get("notice_type"), content.get("sub_type"))
    else:
        print(event_type, content)
```

发送接口已改为显式参数：`send_message(*, message_type, message, group_id=None, user_id=None, **options)`。原来的 `send_message("文本")` 调用不再支持，也不再根据最近接收消息绑定目标。群聊须填 `message_type="group"`、`group_id`、`message`；私聊须填 `message_type="private"`、`user_id`、`message`。目标号码支持正整数或数字字符串；群聊不接受额外的 `user_id`，临时私聊可按 NapCat 需要额外传 `group_id`。缺少必填关键字会抛出 `TypeError`，目标或消息结构无效则返回 `not_sent`，不会提交发送；公共接口的无效请求不会启动子进程。

`message` 支持非空字符串、单个消息段或非空消息段数组，数组内每段须有非空 `type` 与 `data` 对象。消息段及可选参数保持完整传给 NapCat，不转换成纯文本；具体消息段的业务字段和媒体支持由 NapCat 校验和执行。`auto_escape` 可选，默认按 NapCat 的 `false` 语义处理字符串中的 CQ 码；需要将字符串作为纯文本时传 `auto_escape=True`，或使用 `text` 消息段。其他 NapCat 发送选项（例如合并转发的 `source`、`news`、`summary`、`prompt`）可通过关键字传入。适配器不填写消息上报中的 `sender`、`time`、`message_id`，这些不是普通发送请求的必填参数。依据 [NapCat 官方发送实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/SendMsg.ts)。

发送结果仍为 JSON 字符串，仍受启用会话、人工放行和发送回执规则约束。接收的 `content` 是字典；如需模型文本由调用方按需转换，适配器不组装模型角色或会话历史。普通 assistant 文本不自动发送。适配器不导入 Agent 或修改其提示词、工具和权限逻辑。

### 戳一戳

```python
from napcat_adapter.agent_api import send_poke, napcat_receive

# 群内戳指定成员；私聊时省略 group_id。
result = send_poke(user_id=10001, group_id=123456)
result = send_poke(user_id=10001)
# target_id 可选：指定实际被戳者，省略时使用 user_id。
result = send_poke(user_id=10001, target_id=10002)

for event in napcat_receive():
    raw = event["content"]
    if event["type"] == "notice" and raw.get("notice_type") == "notify" and raw.get("sub_type") == "poke":
        actor = raw.get("sender_id", raw["user_id"]) if not raw.get("group_id") else raw["user_id"]
        print(actor, "戳了", raw["target_id"])
```

以上为分段用法，按需选用。`send_poke(*, user_id, group_id=None, target_id=None)` 使用 NapCat 的专用 `send_poke` action，不需要 `message_type` 或 `message`。目标参数必须是正整数或数字字符串；缺参抛 `TypeError`，无效参数返回 `not_sent`，不会启动子进程或提交请求。请求经过现有发送队列，GUI 中目标会话必须启用，手动模式需要人工放行；关闭会话或删除待办会返回 `not_sent`，发送中断线/超时返回 `unknown`，不自动重发。实际执行依赖 NapCat 的 Packet 能力，相关错误按 `failed` 回执返回。成功回执不要求包含 `message_id`，因为戳一戳不是普通聊天消息。参数依据 [NapCat 戳一戳实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/packet/SendPoke.ts)。

戳一戳返回 `{"type": "notice", "content": {...}}`，content 原始事件为 `post_type="notice"`、`notice_type="notify"`、`sub_type="poke"`。群聊的实际操作者取 `user_id`；私聊的会话取 `user_id`，实际操作者优先取 `sender_id`，被戳者均取 `target_id`。通知不要求 `message` 消息段数组，`raw_info` 及未知字段原样保留，不伪造消息编号。

戳一戳使用与其他事件相同的会话启用和接收手动/自动规则，机器人自身发出的事件也可接收。所有入站事件每帧只进入一次接收链路；不按空编号、秒级时间或消息编号丢弃事件，NapCat 若重复上报则逐帧保留。

父进程后台读取管道，不受模型处理阻塞。`napcat_receive()` 立即批量读取本地队列，不等待事件到达，没有网络消息轮询；调用方自行控制轮询频率。界面使用 Tk 定时回调处理跨线程事件。

适配器运行日志显示在适配器“NapCat 配置 → 运行日志”，不会转发到主程序命令行。父进程中的适配器日志也不传播给 Agent 的日志处理器，不修改 Agent 的全局日志设置。子进程 stderr 仍持续读取并保留最近 5 行诊断，避免管道阻塞；启动失败、进程退出等仍通过原接口异常通知主程序。

`start_adapter()` 不重复创建子进程；`stop_adapter()` 可重复调用，并注册 atexit 清理。主程序退出、管道 EOF 或关闭适配器窗口均结束适配器。关闭窗口后主程序会收到适配器退出错误。退出时待发送请求返回未发送，发送中请求在无法取得回执时返回结果未知。

## 环境设置与无界面调试

项目 `.env` 仍由 Agent 自己加载，适配器继承环境，不加载或修改模型配置。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `NAPCAT_GUI` | `1` | 主程序启动图形适配器，`0` 使用无界面协议模式 |
| `NAPCAT_PANEL_CONFIG` | `.config/panel.json` | 图形界面设置路径 |
| `NAPCAT_CONFIG_DIR` | 空 | 自动配置时优先查找的本机 NapCat 目录或 OneBot 配置文件 |
| `NAPCAT_WS_URL` | `ws://127.0.0.1:3001` | 首次启动地址 |
| `NAPCAT_TOKEN` | 空 | 网络 Token，不放在命令行 |
| `NAPCAT_API_TIMEOUT` | `15` | NapCat 回执超时秒数 |
| `NAPCAT_RECONNECT_DELAY` | `3` | 断线重连间隔秒数 |
| `NAPCAT_REQUIRE_AT` | `0` | 兼容旧配置，统一接收不再按 @ 过滤 |
| `NAPCAT_SEND_TIMEOUT` | API 超时 + 5 秒 | 父进程发送等待秒数，人工排队时暂停，GUI 实际发送时采用界面 API 超时 + 5 秒 |
| `NAPCAT_STARTUP_TIMEOUT` | `10` | 子进程就绪超时秒数 |
| `NAPCAT_INBOX_CAPACITY` | `500` | 父进程已放行消息队列容量 |

独立调试，不加载模型或 `.env`：

```powershell
python -m napcat_adapter --gui
# 无界面协议模式，没有人工列表：
python -m napcat_adapter --url ws://127.0.0.1:3001 --group-only --allow-group 123456
```

无界面模式不应用 GUI 放行，所有上报事件进入唯一接收队列，不按 group_only、允许列表、仅 @、自身消息等旧普通接收规则过滤。这些旧选项继续用于发送目标限制或 GUI 会话预填；`require_at` 仅兼容保存，不再限制统一接收。

## 管道协议

stdout 仅输出 JSON 行，日志写 stderr。就绪与消息事件：

```json
{"type":"ready","protocol_version":1}
{"type":"event","source":"qq","conversation_id":"qq:group:123456","text":"你好","raw_event":{"post_type":"message","message_type":"group","group_id":123456,"user_id":10001,"self_id":10002,"message_id":888,"time":1792300000,"message":[{"type":"text","data":{"text":"你好"}}]}}
```

发送命令、人工排队、实际开始和最终回执：

```json
{"type":"send_message","request_id":"req-001","conversation_id":"qq:group:123456","params":{"message_type":"group","group_id":123456,"message":[{"type":"text","data":{"text":"大家好"}}]}}
{"type":"send_pending","request_id":"req-001"}
{"type":"send_started","request_id":"req-001","timeout":20}
{"type":"send_result","request_id":"req-001","conversation_id":"qq:group:123456","success":true,"status":"ok","message_id":"999"}
```

最终 `status` 为 `ok`、`failed`、`not_sent` 或 `unknown`。结果未知不自动重发。使用 `{"type":"shutdown"}` 或关闭 stdin 停止子进程。

发送命令的 `params` 必须包含完整的目标与消息参数，且与 `conversation_id` 一致。旧的仅包含 `text` 的发送命令会被拒绝；GUI 中的 `text` 仅为从完整 `message` 派生的预览，完整发送参数在详情中显示。WebSocket 的 action 与 echo 由适配器生成，分别使用 `send_group_msg` 或 `send_private_msg`。

戳一戳使用相同的进度和最终回执类型，但发送命令为 `send_poke`，不需要正文；通知使用 `notice`，没有唯一消息编号：

```json
{"type":"send_poke","request_id":"poke-001","conversation_id":"qq:group:123456","params":{"group_id":123456,"user_id":10001}}
{"type":"send_result","request_id":"poke-001","conversation_id":"qq:group:123456","action":"send_poke","status":"ok","success":true}
```

内部管道的所有入站事件统一为 `type="event"`，原始事件在 `raw_event`；父进程转换为用户要求的 `{type, content}`，不向调用方暴露 GUI 信封字段。

通用 API 和完整原始事件的管道类型如下。`api_frame` 表示同一 request_id 的中间响应，只有 `send_result` 结束调用；终帧在 `raw_response`，不改写原生 `status`、`retcode`、`echo` 等字段。

```json
{"type":"event","source":"qq","conversation_id":"qq:system:0","text":"[request] friend","raw_event":{"post_type":"request","request_type":"friend","flag":"原始请求标识"}}
{"type":"call_api","request_id":"api-001","conversation_id":"qq:system:0","action":"get_login_info","params":{},"timeout":15,"stream":null}
{"type":"send_result","request_id":"api-001","conversation_id":"qq:system:0","action":"get_login_info","status":"ok","success":true,"data":{"user_id":10002},"raw_response":{"status":"ok","retcode":0,"data":{"user_id":10002},"echo":"网络请求标识"}}
```

父进程只有一个接收队列，受 `NAPCAT_INBOX_CAPACITY` 限制。满时拒绝新事件，`AdapterProcess.dropped_events` 记录数量；GUI 列表的 500 条容量和顶部丢弃计数仍生效。消息/事件不落盘，不提供断线期间的历史事件补发；有可靠保存需求应及时消费并自行保存或使用 NapCat 历史查询 API。

## 维护边界与测试

- 本次界面改造仅修改 `napcat_adapter/`，没有修改 `main.py`、`tools.py` 或其他 Agent Python 文件。
- 运行时只有 `websockets` 一个第三方依赖，GUI 使用标准库 Tk。
- 接收原始事件全部保留，图片、语音等另外生成展示占位符，不下载媒体；发送支持 NapCat 消息段透传，媒体处理由 NapCat 完成。
- 最近 4096 条发送回执在内存去重，不自动重放发送；接收事件不按消息编号去重。消息和人工待办不落盘，退出后不恢复；只有设置持久化。
- 旧 GUI / HTTP 源码备份在 `.artifacts/legacy-gui-http-source.zip`，不参与运行。

```powershell
python -m unittest discover -s napcat_adapter/tests -v
```

测试覆盖人工放行 / 删除、自动模式、会话默认关闭、群聊私聊策略、配置持久化、GUI 三页切换、Token 隐藏、人工等待、真实 GUI 子进程与匿名管道、本地模拟 NapCat 的目录查询和收发、断线重连及退出清理，不使用真实 QQ 或模型。

新增测试覆盖当前筛选结果的全选 / 取消选择、最小窗口布局、一键配置完整按钮流程、原文件精确备份、保留其他服务、已有服务复用、重复配置不重写、多账号选择与 Token 启动读取。所有自动配置测试只使用临时模拟配置文件，不修改实际 NapCat。
