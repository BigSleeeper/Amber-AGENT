# NapCat Python 适配器

由主程序启动的 Python / Tk 图形程序。通过左侧按钮切换主界面、聊天配置、NapCat 配置三个页面。Agent 与适配器使用匿名管道（stdin/stdout、UTF-8 JSONL），适配器与 NapCat 使用正向 WebSocket。无需本机 HTTP 服务、EXE 或消息网络轮询。

```text
QQ ⇄ NapCat ⇄ WebSocket ⇄ Python 适配器子进程（Tk 界面）
                                ⇅ 匿名管道 / JSONL
                          main.py ⇄ Agent / 发送工具
```

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

支持 WebSocket 地址、Token、接口超时、重连间隔、是否只接收 @机器人 的群消息，以及保存、连接、断开和运行日志。默认接收启用群聊的全部成员消息。修改连接参数后，先断开、等待停止，再重新连接。

### 一键自动配置

1. 点击“一键自动配置”。程序优先使用已记住的 NapCat 配置路径，其次查找 `NAPCAT_CONFIG_DIR` 和附近常见 NapCat 目录。未找到或存在多个账号时弹出文件选择框，请选择正在使用的 `onebot11_账号.json`；尚未生成账号配置时可选择 `onebot11.json` 默认模板。
2. 优先复用已开启、消息格式为 `array` 的本机正向 WebSocket 服务。若无兼容服务，新增名为 `lcc-agent-adapter` 的专用服务，监听 `127.0.0.1`，使用可用端口和随机 Token。现有其他服务及配置保留。
3. 修改前在原文件旁创建带时间与随机后缀的 `.bak` 备份；写入前检测并发修改，采用同目录临时文件原子替换。损坏的 JSON 或不符合 NapCat 结构的文件不会被覆盖。备份含原始 Token，应与原配置一样保管；需要恢复时可用备份替换原文件。
4. 自动填写适配器的地址与 Token，保存配置文件路径并连接。**新增或修改 NapCat 服务后需要手动重启 NapCat**；界面会明确提示，适配器持续重连等待服务生效。不会自动重启 QQ / NapCat，也不会安装或登录 NapCat。

账号专属配置优先于默认模板，多账号时不猜测账号。`onebot11.json` 是 NapCat 默认模板，已有账号专属文件时请选专属文件。只支持标准 UTF-8 JSON；含 JSON5 注释的手写文件需先在 NapCat 中保存为 JSON。配置格式依据 [NapCat 官方配置指南](https://doc.napneko.icu/config/basic)。

适配器配置保存在本目录 `.config/panel.json`（Git 忽略），也可通过 `NAPCAT_PANEL_CONFIG` 指定路径。**Token 不复制写入适配器配置文件**。自动配置生成的 Token 保存在 NapCat 自己的服务配置中；适配器下次启动从已选择的文件读取与已保存地址匹配的 Token 到内存，环境变量 `NAPCAT_TOKEN` 非空时优先使用。手动输入且不在 NapCat 文件中的 Token 仍只在本次进程内保存。已保存的界面连接设置优先于环境提供的初始地址及超时。

## Agent 接口和生命周期

```python
from napcat_adapter.agent_api import (
    start_adapter, stop_adapter, receive_message, send_message,
)
start_adapter()
try:
    message = receive_message()  # dict 或 None，只取已放行的消息
    if message:
        print(message["conversation_id"], message["content"])
        result = send_message("收到")  # JSON 字符串，目标绑定当前 QQ 会话
finally:
    stop_adapter()
```

接口签名保持不变。消息仍以 `role: user` 进入主循环，会话历史由 `main.py` 管理。模型通过发送工具发 QQ 消息，普通 assistant 文本不自动发送。适配器不导入 Agent 或修改其提示词、工具和权限逻辑。

父进程后台读取管道，不受模型处理阻塞。`receive_message()` 等待本地队列，默认最多 1 秒，便于 Windows 主循环响应 Ctrl+C；消息到达立即唤醒，没有网络消息轮询。界面使用 Tk 定时回调处理跨线程事件。

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
| `NAPCAT_REQUIRE_AT` | `0` | 首次启动是否要求群消息 @机器人 |
| `NAPCAT_SEND_TIMEOUT` | API 超时 + 5 秒 | 父进程发送等待秒数，人工排队时暂停，GUI 实际发送时采用界面 API 超时 + 5 秒 |
| `NAPCAT_STARTUP_TIMEOUT` | `10` | 子进程就绪超时秒数 |
| `NAPCAT_INBOX_CAPACITY` | `500` | 父进程已放行消息队列容量 |

独立调试，不加载模型或 `.env`：

```powershell
python -m napcat_adapter --gui
# 无界面协议模式，没有人工列表：
python -m napcat_adapter --url ws://127.0.0.1:3001 --group-only --allow-group 123456
```

主程序无界面模式仍使用旧过滤规则：`NAPCAT_GROUP_ONLY` 默认 `1`；`NAPCAT_ALLOW_GROUPS` 空表示全部群；`NAPCAT_ALLOW_USERS` 空表示全部成员。调试时需明确所用模式。

## 管道协议

stdout 仅输出 JSON 行，日志写 stderr。就绪与消息事件：

```json
{"type":"ready","protocol_version":1}
{"type":"message","source":"qq","conversation_id":"qq:group:123456","sender_id":"10001","message_id":"888","text":"你好","timestamp":1792300000,"metadata":{}}
```

发送命令、人工排队、实际开始和最终回执：

```json
{"type":"send_message","request_id":"req-001","conversation_id":"qq:group:123456","text":"大家好"}
{"type":"send_pending","request_id":"req-001"}
{"type":"send_started","request_id":"req-001","timeout":20}
{"type":"send_result","request_id":"req-001","conversation_id":"qq:group:123456","success":true,"status":"ok","message_id":"999"}
```

最终 `status` 为 `ok`、`failed`、`not_sent` 或 `unknown`。结果未知不自动重发。使用 `{"type":"shutdown"}` 或关闭 stdin 停止子进程。

## 维护边界与测试

- 本次界面改造仅修改 `napcat_adapter/`，没有修改 `main.py`、`tools.py` 或其他 Agent Python 文件。
- 运行时只有 `websockets` 一个第三方依赖，GUI 使用标准库 Tk。
- 图片、语音保留消息段与占位符，不下载媒体，发送支持纯文本。
- 最近 4096 条消息及发送回执在内存去重，不自动重放发送。消息和人工待办不落盘，退出后不恢复；只有设置持久化。
- 旧 GUI / HTTP 源码备份在 `.artifacts/legacy-gui-http-source.zip`，不参与运行。

```powershell
python -m unittest discover -s napcat_adapter/tests -v
```

测试覆盖人工放行 / 删除、自动模式、会话默认关闭、群聊私聊策略、配置持久化、GUI 三页切换、Token 隐藏、人工等待、真实 GUI 子进程与匿名管道、本地模拟 NapCat 的目录查询和收发、断线重连及退出清理，不使用真实 QQ 或模型。

新增测试覆盖当前筛选结果的全选 / 取消选择、最小窗口布局、一键配置完整按钮流程、原文件精确备份、保留其他服务、已有服务复用、重复配置不重写、多账号选择与 Token 启动读取。所有自动配置测试只使用临时模拟配置文件，不修改实际 NapCat。
