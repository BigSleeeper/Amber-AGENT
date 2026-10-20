"""NapCat 适配器：协议与传输不依赖 Agent，可单独作为 JSONL 子进程运行。"""

from .client import NapCatAdapter
from .protocol import normalize_event

__all__ = ["NapCatAdapter", "normalize_event"]
