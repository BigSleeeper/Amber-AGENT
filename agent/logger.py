import shutil
import unicodedata

def log_out(*args):
    """用空格连接多个内容参数，最后一个位置参数指定预览字符上限。"""
    if len(args) < 2:
        raise TypeError("至少需要一个内容参数和最后的输出长度参数")
    *logs, length = args
    if not isinstance(length, int) or isinstance(length, bool):
        raise TypeError("最后一个参数（输出长度）必须是整数")
    if length <= 0:
        raise ValueError("length 必须大于 0")
    text = " ".join(" ".join(str(log) for log in logs).split()) or "无输出"
    columns = shutil.get_terminal_size().columns - 1
    while length >= 0:
        preview = text[:length]
        if len(text) > length:
            preview += f" ...省略 {len(text) - length} 个字符"
        if sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in preview) <= columns:
            break
        length -= 1
    if length < 0:
        preview = "..."[:max(columns, 0)]
    print(preview)

