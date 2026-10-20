import re

from config import WORKDIR
import utils

HOOKS = {
    "UserPromptSubmit": [], 
    "PreTurn":[], 
    "PreToolUse": [], 
    "PostToolUse": [], 
    "Stop": []
}

def register_hook(event, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE_COMMAND_WORD = re.compile(r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])")
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))

def permission_hook(tool_name: str, args: dict):
    """PreToolUse: s03 check_permission() logic moved here."""
    if tool_name == "bash":
        command = args.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n{utils.term.red}[blocked] '{pattern}'{utils.term.normal}")
                return "Permission denied by deny list"
        if contains_destructive_command(command) or any(
            kw in command for kw in DESTRUCTIVE
        ):
            print(f"\n{utils.term.yellow}[permission] Potentially destructive command{utils.term.normal}")
            print(f"   Tool: {tool_name}({args})")
            choice = utils.list_box("是否允许执行？",["是","否"])
            if choice != "是":
                return "Permission denied by user"
    if tool_name in ("read_file", "write_file", "edit_file"):
        path = args.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n{utils.term.yellow}[permission] Access outside workspace{utils.term.normal}")
            print(f"   Tool: {tool_name}({args})")
            choice = utils.list_box("是否允许执行？",["是","否"])
            if choice != "是":
                return "Permission denied by user"
    return None

def log_tool_use(tool_name: str, args: dict):
    text = "使用工具 " + tool_name + " " + str(list(args.values()))
    utils.preview_text(text,1)
    return None

def log_tool_out(tool_name: str, args: dict, output):
    text = "工具返回 " + tool_name + " " + str(output)
    utils.preview_text(text,1)
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    print(f"{utils.term.bright_black}[HOOK] UserPromptSubmit: working in {WORKDIR}{utils.term.normal}")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"{utils.term.bright_black}[HOOK] Stop: session used {tool_count} tool calls{utils.term.normal}")
    return None

def pre_turn_log(S, T):
    utils.separator_line(S, T)
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreTurn", pre_turn_log)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_tool_use)
register_hook("PostToolUse", log_tool_out)
register_hook("Stop", summary_hook)