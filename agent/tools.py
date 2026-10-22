import os
import re
import ast
import json
import subprocess

from .config import MODEL,WORKDIR,ENVIRONMENT_PROMPT
from .client import client
from .hooks import *

from napcat_adapter.agent_api import send_message

# ===========================工具===========================
# 基本工具
BASE_TOOLS = []
# 高级工具
ADVANCED_TOOLS = []

# 命令行工具
BASE_TOOLS.append({
    "type":"function",
    "function":{
        "name":"bash",
        "description":"运行一条命令行指令",
        "parameters":{
            "type":"object",
            "properties":{"command":{"type":"string"}},
            "required":["command"],
        },
    },
})
def run_bash(command):
    try:
        r = subprocess.run(command, shell=True,
                           cwd=os.getcwd(),
                           capture_output=True,
                           text=True,
                           errors="replace",
                           timeout=120
                           )
        out = (r.stdout + r.stderr).strip() # 删除首位空白字符
        if out:
            return out[:50000]
        else:
            return "无输出"

    except subprocess.TimeoutExpired:
        return "指令执行超时！(120s)"

    except (FileNotFoundError, OSError) as e:
        return f"Error:{e}"

# 文件读取工具
BASE_TOOLS.append({
    "type":"function",
    "function":{
        "name":"read_file",
        "description":"读取文件内容",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{"type":"string"},
                "limit":{"type":"integer"}
            },
        "required":["path","limit"],
        }
    }
})
def run_read(path, limit):
    try:
        file_path = (WORKDIR / path).resolve()
        lines = file_path.read_text(encoding="utf-8").splitlines()

        if len(lines) > limit:
            lines = lines[:limit] + [f"...（剩余 {len(lines)-limit} 行）"]

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"

# 文件写入工具
BASE_TOOLS.append({
    "type":"function",
    "function":{
        "name":"write_file",
        "description":"在指定文件中写入内容",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{"type":"string"},
                "content":{"type":"string"}
            },
            "required":["path","content"]
        }
    }
})
def run_write(path, content):
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

# 文件编辑工具
BASE_TOOLS.append({
    "type":"function",
    "function":{
        "name":"edit_file",
        "description":"替换文件中的指定文本",
        "parameters":{
            "type":"object",
            "properties":{
                "path":{"type":"string"},
                "old_text":{"type":"string"},
                "new_text":{"type":"string"}
            },
            "required":["path","old_text","new_text"]
        }
    }
})
def run_edit(path, old_text, new_text):
    try:
        file_path = (WORKDIR/path).resolve()
        text = file_path.read_text(encoding='utf-8') #一次性读取整个文件
        if old_text in text:
            file_path.write_text(text.replace(old_text,new_text,1), encoding="utf-8")
            return f"文件 {path} 修改完成。"
        else:
            return f"错误: 未在文件 {path} 中找到要被替换的文本"

    except Exception as e:
        return f"Error: {e}"

# 文件搜索工具
BASE_TOOLS.append({
    "type":"function",
    "function":{
        "name":"glob",
        "description":"查找匹配某个通配符模式的文件；**表示递归匹配。",
        "parameters":{
            "type":"object",
            "properties":{
                "pattern":{"type":"string"}
            },
            "required":["pattern"]
        }
    }
})
def run_glob(pattern):
    import glob as g
    try:
        matches = g.glob(pattern, root_dir=WORKDIR, recursive=True)
        
        if not matches:
            return "(无匹配项)"
        
        matches = {match for match in matches if (WORKDIR/match).resolve().is_relative_to(WORKDIR)} # 使用集合自动去重
        matches = sorted(matches) # 排序，并把集合set转为列表list
        shown = matches[0:100]

        if len(matches) > 100:
            shown.append(f"...共{len(matches)}个匹配项，其余已省略，请缩小模式范围。")
        return "\n".join(shown)
    except Exception as e:
        return f"Error: {e}"

class TodoManager:
    def __init__(self):
        self.items: list[dict] = []

    def update(self, todos: list | str) -> str:
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as e:
                    raise ValueError("todos must be a list or JSON array string") from e

        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        if len(todos) > 20:
            raise ValueError("Max 20 todos allowed")

        validated = []
        in_progress_count = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")

            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"content": content, "status": status})

        if in_progress_count > 1:
            raise ValueError("Only one todo can be in_progress at a time")

        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."

        lines = []
        for todo in self.items:
            marker = {
                "pending": "[  ]",
                "in_progress": "[>>]",
                "completed": "[OK]",
            }[todo["status"]]
            lines.append(f"{marker} {todo['content']}")

        done = sum(todo["status"] == "completed" for todo in self.items)
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

TODO = TodoManager()

# 任务拆解工具
ADVANCED_TOOLS.append({
    "type":"function",
    "function":{
        "name":"todo_write",
        "description":"为当前任务创建并管理一个任务列表",
        "parameters":{
            "type":"object",
            "properties":{
                "todos":{
                    "type":"array",
                    "maxItems":20,
                    "items":{
                        "type":"object",
                        "properties":{
                            "content":{
                                "type":"string",
                                "minLength":1,
                            },
                            "status":{
                                "type":"string",
                                "enum":["pending","in_progress","completed"]
                            }
                        },
                        "required":["content","status"]
                    }
                }
            },
            "required":["todos"]
        }
    }
})
def run_todo_write(todos):
    try:
        output = TODO.update(todos)
    except ValueError as e:
        return f"Error: {e}"
    print(f"当前任务列表：\n{output}")
    return output


# 子agent工具
ADVANCED_TOOLS.append({
    "type":"function",
    "function":{
        "name":"task",
        "description":"使用新的上下文运行一个子agent，返回最终的文本。",
        "parameters":{
            "type":"object",
            "properties":{
                "prompt":{
                    "type":"string",
                    "minLength":1
                }
            },
            "required":["prompt"]
        }
    }
})
def run_subagent(prompt):
    print("启动子agent")
    SUB_AGENT_TOOLS = list(BASE_TOOLS)
    SUB_HANDLERS = dict(BASE_TOOL_HANDLERS)
    SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Complete the given task, then return a concise final answer."
    )
    messages=[
        {"role":"system", "content":SUB_SYSTEM},
        {"role":"user", "content":prompt}
    ]

    # 设置子agent的轮次上限为30轮
    for _ in range(30):
        response = client.chat.completions.create(
            model = MODEL,
            messages = messages,
            tools = SUB_AGENT_TOOLS,
            max_completion_tokens=8000
        )

        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        tool_calls = message.tool_calls or []
        if not tool_calls:
            return response.choices[0].message.content

        results = []
        for tool_call in tool_calls:
            output = execute_tool(tool_call)

            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(results)
    return "子代理在30轮后停止，未给出最终答案。"

# 上下文压缩工具
ADVANCED_TOOLS.append({
    "type":"function",
    "function":{
        "name":"compact",
        "description":"总结之前的对话，以释放上下文空间。",
        "parameters":{
            "type":"object",
            "properties":{}
        }
    }
})
'''该工具的函数体在context.py中，须在主循环中实现'''

# 发送QQ消息工具
ADVANCED_TOOLS.append({
    "type":"function",
    "function":{
        "name":"send_QQ_message",
        "description":"发送一条QQ消息到指定 qid 的私聊/群聊中",
        "parameters":{
            "type":"object",
            "properties":{
                "message_type":{"type":"string", "enum":["private", "group"]},
                "qid":{"type":"string","enum":["2162075257","123456"]},
                "message":{"type":"string","minLength":1}
            },
            "required":["message_type", "qid", "message"]
        }
    }
})
def run_send(message_type, qid, message):
    if message_type == "private":
        return send_message(
            message_type="private",
            group_id=qid,
            message=message
            )

    if message_type == "private":
        return send_message(
            message_type="private",
            group_id=qid,
            message=message
            )

# 工具分发
BASE_TOOL_HANDLERS = {
    "bash": run_bash, 
    "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}
ADVANCED_TOOL_HANDLERS = {
    "todo_write":run_todo_write,
    "task":run_subagent,
    "send_QQ_message":run_send
}

MAIN_AGENT_TOOLS = [*BASE_TOOLS, *ADVANCED_TOOLS]
MAIN_HANDLERS = {**BASE_TOOL_HANDLERS, **ADVANCED_TOOL_HANDLERS}

def execute_tool(tool_call):
    try:
        # 获取调用的工具的参数
        arguments = json.loads(tool_call.function.arguments)
        # 获取调用的工具的名称
        tool_name = tool_call.function.name
        # 映射工具对应函数
        handler = MAIN_HANDLERS.get(tool_name)
    except Exception as e:
        output = f"Error: {e}"
        return output

    # 工具调用前置钩子
    blocked = trigger_hooks("PreToolUse", tool_name, arguments)
    # 如果钩子函数有返回值，说明工具调用被禁止
    if blocked:
        return str(blocked)

    # arguments是大模型返回的字典 {"参数名称":"参数值"}
    try:
        output = handler(**arguments)           # 传递键值对
        #output = handler(*arguments.values())  # 仅传递值，要求大模型必须按顺序填写参数
    except Exception as e:
        output = f"Error: {e}"

    # 工具调用后置钩子
    trigger_hooks("PostToolUse", tool_name, arguments, output)

    return output
