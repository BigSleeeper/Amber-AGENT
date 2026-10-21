import json

from .client import create_completion
from .config import MODEL, SYSTEM
from .tools import *
from .hooks import *
from .context import COMPACTOR,MAX_REACTIVE_RETRIES
from .memory import *

# 核心AGENT循环
agent_cnt = [0,0]
def agent_loop(messages:list, active_request: str):
    relevant_memories = load_memories(messages)
    system = build_system(relevant_memories)

    global agent_cnt
    rounds_since_todo = 0
    reactive_retries = 0
    while True:
        # 轮次计数
        agent_cnt[1] += 1
        # 轮次前置钩子
        trigger_hooks("PreTurn",agent_cnt[0],agent_cnt[1])

        # 准备上下文
        # 列表的切片赋值，可以修改函数传入的列表变量
        messages[:] = COMPACTOR.prepare(messages, active_request)

        # 发送LLM请求
        try:
            response = create_completion(
                model = MODEL,
                messages = [{"role":"system","content":system},*messages],
                tools = MAIN_AGENT_TOOLS,
                max_completion_tokens = 8000,
                )
        except Exception as e:
            # 判断是否超出上下文限制
            too_long = any(
                text in str(e).lower() for text in [
                        "prompt_too_long",
                        "too many tokens",
                        "context_length_exceeded"
                    ]
                )
            # 如果超出了，并且压缩次数没有超限，就继续压缩
            if too_long and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[再次压缩上下文]")
                messages[:] = COMPACTOR.reactive_compact(messages, active_request)
                reactive_retries += 1
                continue
            raise

        # 把大模型返回消息添加到上下文
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        #print("\n",message.model_dump(exclude_none=True),"\n")

        # 判断是否要调用工具
        tool_calls = message.tool_calls or []

        # 如果不需要调用工具，则认为任务已经完成
        if not tool_calls:
            if extract_memories(messages):
                consolidate_memories()
            return

        # 依次完成所有工具调用请求
        results = []
        used_todo = False # todos更新标志位
        compact_requested = False # 上下文压缩标志位
        for tool_call in tool_calls:
            if tool_call.function.name == "todo_write":
                used_todo = True

            if tool_call.function.name == "compact":
                output = "即将压缩上下文"
                compact_requested = True
            else:
                output = execute_tool(tool_call)
                results.append({"role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": output,
                                })                         

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # 如果连续三个轮次没有更新todo列表，就提醒大模型
        if rounds_since_todo >= 3:
            results.append({
                "role":"user",
                "content":"<reminder> Update your todos. </reminder>"
            })

        messages.extend(results)

        if compact_requested:
            messages[:] = COMPACTOR.compact_history(messages, active_request)