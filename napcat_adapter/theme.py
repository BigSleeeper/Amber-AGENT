"""接近 Codex 的中性灰阶主题，支持运行中切换，不依赖第三方 UI 包。"""

import sys
from tkinter import ttk


FONT = "Microsoft YaHei UI"
PALETTES = {
    "light": dict(bg="#ffffff", card="#ffffff", sidebar="#f7f7f5", ink="#242424",
                  muted="#73736f", disabled="#aaa9a4", line="#e5e5e0", focus="#92928c",
                  hover="#efefeb", selected="#e7e7e2", selected_ink="#202020", odd="#fbfbf9",
                  input="#fafaf8", button="#f0f0ed", primary="#252525", primary_hover="#444440",
                  primary_ink="#ffffff", danger_bg="#fff1f1", danger_hover="#fbe2e2",
                  green="#25805a", amber="#9c6a15", red="#ba4646", scroll="#ccccc6",
                  log_bg="#f7f7f4", log_ink="#50504b"),
    "dark": dict(bg="#1b1b1b", card="#202020", sidebar="#151515", ink="#e7e7e3",
                 muted="#a3a39a", disabled="#666660", line="#363632", focus="#85857b",
                 hover="#2b2b28", selected="#3b3b35", selected_ink="#f4f4ef", odd="#242423",
                 input="#262625", button="#30302c", primary="#e5e5df", primary_hover="#ffffff",
                 primary_ink="#1b1b19", danger_bg="#352323", danger_hover="#493030",
                 green="#79c49a", amber="#d6b16b", red="#e68b8b", scroll="#53534c",
                 log_bg="#191919", log_ink="#c4c4ba"),
}


def palette_for(name):
    if name not in PALETTES:
        raise ValueError("主题必须为 light 或 dark")
    return PALETTES[name].copy()


def style_titlebar(root, name):
    """Windows 10/11 支持时同步原生标题栏，其他平台保持系统样式。"""
    if sys.platform != "win32" or not root.winfo_ismapped():
        return
    try:
        import ctypes
        from ctypes import wintypes
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
        ancestor.restype = wintypes.HWND
        handle = ancestor(root.winfo_id(), 2)
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        set_attribute.restype = wintypes.LONG
        dark = ctypes.c_int(name == "dark")
        if set_attribute(handle, 20, ctypes.byref(dark), ctypes.sizeof(dark)) != 0:
            set_attribute(handle, 19, ctypes.byref(dark), ctypes.sizeof(dark))
    except (AttributeError, OSError):
        pass


def apply_theme(root, name="light"):
    colors = palette_for(name)
    root.configure(bg=colors["bg"])
    style = ttk.Style(root)
    # 仅首次选择底层主题，切换颜色不重置已有控件的状态。
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure(".", font=(FONT, 10), background=colors["bg"], foreground=colors["ink"])
    style.configure("TFrame", background=colors["bg"])
    style.configure("TLabel", background=colors["bg"], foreground=colors["ink"])
    style.configure("Header.TFrame", background=colors["bg"])
    style.configure("Header.TLabel", background=colors["bg"], foreground=colors["muted"])
    style.configure("Title.TLabel", font=(FONT, 18, "bold"))
    style.configure("Hint.TLabel", foreground=colors["muted"], font=(FONT, 9))
    style.configure("TSeparator", background=colors["line"])
    style.configure("Card.TFrame", background=colors["card"], borderwidth=1,
                    relief="solid", bordercolor=colors["line"], lightcolor=colors["line"], darkcolor=colors["line"])
    style.configure("CardBody.TFrame", background=colors["card"])
    style.configure("Card.TLabel", background=colors["card"], foreground=colors["ink"])
    style.configure("CardHint.TLabel", background=colors["card"], foreground=colors["muted"])
    style.configure("CardTitle.TLabel", background=colors["card"], foreground=colors["ink"], font=(FONT, 11, "bold"))
    style.configure("Metric.TLabel", background=colors["card"], foreground=colors["ink"], font=(FONT, 19, "bold"))
    style.configure("Card.TLabelframe", background=colors["card"], bordercolor=colors["line"], borderwidth=1, relief="solid")
    style.configure("Card.TLabelframe.Label", background=colors["bg"], foreground=colors["muted"], font=(FONT, 9))
    style.configure("TButton", background=colors["button"], foreground=colors["ink"], padding=(13, 8),
                    borderwidth=0, focusthickness=1, focuscolor=colors["focus"], font=(FONT, 10))
    style.map("TButton", background=[("disabled", colors["button"]), ("active", colors["hover"])],
              foreground=[("disabled", colors["disabled"])])
    style.configure("Primary.TButton", background=colors["primary"], foreground=colors["primary_ink"])
    style.map("Primary.TButton", background=[("disabled", colors["button"]), ("active", colors["primary_hover"])],
              foreground=[("disabled", colors["disabled"]), ("active", colors["primary_ink"])])
    style.configure("Danger.TButton", background=colors["danger_bg"], foreground=colors["red"])
    style.map("Danger.TButton", background=[("disabled", colors["button"]), ("active", colors["danger_hover"])],
              foreground=[("disabled", colors["disabled"]), ("active", colors["red"])])
    style.configure("Small.TButton", padding=(9, 5), font=(FONT, 9))
    style.configure("SidebarChoice.TButton", background=colors["sidebar"], foreground=colors["muted"], padding=(12, 7), font=(FONT, 9))
    style.map("SidebarChoice.TButton", background=[("active", colors["hover"])], foreground=[("active", colors["ink"])])
    style.configure("SidebarSelected.TButton", background=colors["selected"], foreground=colors["ink"], padding=(12, 7), font=(FONT, 9))
    style.map("SidebarSelected.TButton", background=[("active", colors["selected"])], foreground=[("active", colors["ink"])])
    style.configure("Mode.TRadiobutton", background=colors["card"], padding=(5, 7), foreground=colors["muted"],
                    indicatorcolor=colors["button"], indicatorforeground=colors["card"])
    style.map("Mode.TRadiobutton", background=[("active", colors["card"])],
              foreground=[("disabled", colors["disabled"]), ("selected", colors["ink"])],
              indicatorcolor=[("selected", colors["primary"])], indicatorforeground=[("selected", colors["primary_ink"])])
    style.configure("Card.TCheckbutton", background=colors["card"], foreground=colors["ink"], padding=(0, 6),
                    indicatorcolor=colors["input"], indicatorforeground=colors["ink"])
    style.map("Card.TCheckbutton", background=[("active", colors["card"])],
              indicatorcolor=[("selected", colors["primary"])], indicatorforeground=[("selected", colors["primary_ink"])])
    style.configure("TEntry", padding=9, fieldbackground=colors["input"], foreground=colors["ink"],
                    bordercolor=colors["line"], lightcolor=colors["line"], darkcolor=colors["line"], insertcolor=colors["ink"])
    style.map("TEntry", bordercolor=[("focus", colors["focus"])], foreground=[("disabled", colors["disabled"])])
    style.configure("TCombobox", padding=7, background=colors["button"], fieldbackground=colors["input"],
                    foreground=colors["ink"], arrowcolor=colors["muted"], bordercolor=colors["line"],
                    lightcolor=colors["line"], darkcolor=colors["line"], insertcolor=colors["ink"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["input"])],
              foreground=[("disabled", colors["disabled"]), ("readonly", colors["ink"])],
              background=[("active", colors["hover"])], bordercolor=[("focus", colors["focus"])])
    for option, value in (("background", colors["card"]), ("foreground", colors["ink"]),
                          ("selectBackground", colors["selected"]), ("selectForeground", colors["selected_ink"])):
        root.option_add("*TCombobox*Listbox." + option, value)
    style.configure("Treeview", background=colors["card"], fieldbackground=colors["card"], foreground=colors["ink"],
                    rowheight=34, borderwidth=0, font=(FONT, 10))
    style.map("Treeview", background=[("selected", colors["selected"])], foreground=[("selected", colors["selected_ink"])])
    style.configure("Treeview.Heading", background=colors["input"], foreground=colors["muted"], padding=(10, 10),
                    relief="flat", borderwidth=0, font=(FONT, 9))
    style.map("Treeview.Heading", background=[("active", colors["hover"])])
    style.configure("Vertical.TScrollbar", background=colors["scroll"], troughcolor=colors["card"], borderwidth=0,
                    lightcolor=colors["scroll"], darkcolor=colors["scroll"], arrowcolor=colors["muted"], arrowsize=12)
    style.map("Vertical.TScrollbar", background=[("active", colors["muted"]), ("pressed", colors["muted"])])
    style.configure("TPanedwindow", background=colors["bg"], sashwidth=14)
    style_titlebar(root, name)
    return style
