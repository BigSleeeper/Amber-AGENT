"""NapCat 适配器：协议与传输不依赖 Agent，可单独作为 JSONL 子进程运行。"""

from .client import NapCatAdapter
from .protocol import NapCatReceiveType, normalize_event
from .agent_api import napcat_receive

__all__ = ["NapCatAdapter", "NapCatReceiveType", "napcat_receive", "normalize_event"]
