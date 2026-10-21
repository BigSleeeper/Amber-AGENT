import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(override=True)

# 工作目录
WORKDIR = Path.cwd()

# 上下文压缩目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool_results"

# 记忆目录
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 环境信息
ENVIRONMENT_PROMPT = (
    "操作系统：Windows 10;"
    "bash工具通过cmd.exe执行，遵循cmd.exe的格式，而不是Unix、Bash或Powershell;"
    if os.name == "nt" else
    "操作系统：类Unix系统;"
    "bash 工具会调用系统原生 shell"
)

# 系统提示词
SYSTEM = (
    f"你是一个群聊agent，你的工作目录在 {WORKDIR} 中。"
    f"环境：{ENVIRONMENT_PROMPT}。"
    f"你通过消息发送工具向群聊中发送消息，当消息发送完成，并且无后续任务时，输出'回复完成'"
)

BASE_URL = os.getenv("OPENAI_BASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL_ID")
