from blessed import Terminal

term = Terminal()

# 分割线
def separator_line(S, T):
    text = "S%d T%d"%(S,T)
    n = (term.width - term.length(text) - 1) // 2
    if T == 1:
        text = n * '=' + text + n * '='
    else:
        text = n * '-' + text + n * '-'
    print(text,'\n')

# 接续上文单选
def list_box(prompt:str, options:list, empty:str="未选择"):
    # 没有候选项时直接返回取消值，避免访问空列表。
    if not options:
        return empty
    sel = 0
    # 进入按键检测模式，隐藏光标
    with term.cbreak(), term.hidden_cursor():
        # 记录菜单显示前的光标行、列位置，最多等待 1 秒。
        cursor_y, cursor_x = term.get_location(timeout=1)
        # 查询失败会返回负坐标，此时不能继续定位菜单。
        if cursor_y < 0 or cursor_x < 0:
            raise RuntimeError("无法获取终端光标位置，请在交互式终端中运行")
        # 当前行已有内容时，从下一行显示菜单，避免覆盖上文。
        start = cursor_y + (cursor_x > 0)
        while True:
            # 每次重绘都读取终端尺寸，以当前宽度重新计算换行。
            h,w = term.height, term.width
            menu = ""
            for idx, val in enumerate(options):
                # 选中项使用反色，normal 将后续选项恢复为普通样式。
                if idx == sel:
                    menu += f"[ {term.reverse}{val}{term.normal} ]"
                else:
                    menu += f"[ {val} ]"
            # 同时计算提示与选项的显示宽度；限制高度，并留一列避免自动折行。
            lines = term.wrap(f"{prompt} {menu}", width=max(w - 1, 1))[-max(h - 1, 1):]
            # 菜单超出屏幕底部时，计算需要滚动腾出的行数。
            scroll = max(0, start + len(lines) - h)
            if scroll:
                # 在最后一行输出换行，让上文进入滚动历史，腾出空行。
                print(term.move_yx(h - 1,0), "\n" * scroll, sep="", end="", flush=True)
                # 屏幕滚动后，恢复位置和菜单起始行也要同步上移。
                cursor_y = max(0, cursor_y - scroll)
                start -= scroll
            # 先回到更新后的原位置，再保存光标，避免恢复到滚动前的旧坐标。
            print(term.move_yx(cursor_y,cursor_x), end="", flush=True)
            # 离开此上下文时（包括 return），自动恢复绘制前的光标位置。
            with term.location():
                for row, line in enumerate(lines):
                    # 清除旧菜单行并立即刷新，确保等待按键前菜单已经可见。
                    print(term.move_yx(start + row,0), term.clear_eol, line, sep="", end="", flush=True)

                key = term.inkey()

                if key.name == "KEY_LEFT":
                    sel = sel - 1
                elif key.name == "KEY_RIGHT":
                    sel = sel + 1
                # Enter 确认；Esc 或 Alt+Esc 取消。
                elif key.name in ("KEY_ENTER", "KEY_ESCAPE", "KEY_ALT_ESCAPE"):
                    # 退出前仅清除菜单占用的行，不清除上文。
                    for row in range(len(lines)):
                        print(term.move_yx(start + row,0), term.clear_eol, sep="", end="")
                    return options[sel] if key.name == "KEY_ENTER" else empty

                # 将选项索引限制在有效范围，防止左右键越界。
                sel = max(0,min(len(options)-1,sel))

def preview_text(text, n):
    if term.width < 30:
        print("窗口过窄！")
        return None

    text = ' '.join(text.split())
    lines = term.wrap(text, width=term.width - 1)
    if n >= len(lines):
        for i in lines:
            print(i)
    else:
        foldcnt = 0
        for i in range(n,len(lines)):
            foldcnt += len(lines[i])

        tail = f"...({foldcnt} more.)"
        while term.length(lines[n-1] + tail) >= term.width:
            lines[n-1] = lines[n-1][:-1]
            foldcnt += 1
            tail = f"...({foldcnt} more.)"
        lines[n-1] += tail
        for i in range(n):
            print(lines[i])

    return None