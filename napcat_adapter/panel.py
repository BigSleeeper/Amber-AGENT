"""纯 Python / Tk 管理界面。Tk、策略在主线程，WebSocket 在后台事件循环。"""

import asyncio
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .client import NapCatAdapter
from .panel_model import PanelModel, Settings
from .protocol import SYSTEM_CONVERSATION, send_result, validate_send_command
from .auto_config import configure_local, find_configs, saved_token
from .theme import FONT, apply_theme, palette_for, style_titlebar


class Network:
    def __init__(self, events):
        self.events, self.thread, self.loop, self.adapter = events, None, None, None
        self.lock = threading.Lock()
        self.stop_requested = False
        self.outstanding = {}

    def emit(self, event):
        if event.get("type") == "send_result":
            with self.lock:
                self.outstanding.pop(event.get("request_id"), None)
        self.events.put(event)

    def start(self, connection, token):
        if self.thread and self.thread.is_alive():
            raise ValueError("请先断开当前连接，等待状态变为已停止后再连接")
        self.adapter = NapCatAdapter(token=token, **connection)
        self.adapter.report_progress = True
        self.stop_requested = False
        def worker():
            try:
                def ready(loop, accept, stop):
                    with self.lock:
                        self.loop = loop
                        if self.stop_requested:
                            stop()
                asyncio.run(self.adapter.run(self.emit, ready))
            except Exception as exc:
                self.events.put(dict(type="log", text=f"网络线程退出：{type(exc).__name__}"))
            finally:
                with self.lock:
                    self.loop = None
                    unresolved = list(self.outstanding.values())
                    self.outstanding.clear()
                for command in unresolved:
                    self.events.put(send_result(command, "unknown", error="连接已停止，发送结果未知；不要自动重发"))
                self.events.put(dict(type="status", status="stopped"))
        self.thread = threading.Thread(target=worker, name="napcat-websocket", daemon=True)
        self.thread.start()

    def submit(self, command):
        with self.lock:
            if self.loop is not None and self.loop.is_running():
                try:
                    self.loop.call_soon_threadsafe(self.adapter.accept_command, command)
                    self.outstanding[command["request_id"]] = command
                    return
                except RuntimeError:
                    pass
        self.events.put(send_result(command, "not_sent", error="NapCat 尚未启动"))

    def stop(self):
        with self.lock:
            self.stop_requested = True
            if self.loop is not None and self.loop.is_running():
                try:
                    self.loop.call_soon_threadsafe(self.adapter.accept_command, dict(type="shutdown"))
                except RuntimeError:
                    pass

    def catalog(self):
        async def fetch(adapter):
            for kind, action in (("group", "get_group_list"), ("private", "get_friend_list")):
                try:
                    items = await adapter.call_api(action)
                    self.events.put(dict(type="catalog", kind=kind, items=items))
                except Exception as exc:
                    self.events.put(dict(type="log", text=f"获取{('群聊' if kind == 'group' else '好友')}列表失败：{type(exc).__name__}"))
        with self.lock:
            if self.loop is not None and self.loop.is_running():
                coroutine = fetch(self.adapter)
                try:
                    asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                except RuntimeError:
                    coroutine.close()


class QueueLog(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(dict(type="log", text=self.format(record)))


class Panel:
    STATES = dict(pending="等待放行", sending="发送中", delivered="已交给 Agent", ok="发送成功",
                  not_sent="未发送", failed="发送失败", unknown="结果未知")

    def __init__(self, root, settings, emit, *, auto_connect=True, token=""):
        self.root, self.settings = root, settings
        self._external_emit = emit
        self.emit = self.emit_event
        self.events = queue.SimpleQueue()
        self.network = Network(self.events)
        self.model = PanelModel(settings, self.emit, self.network.submit)
        self.closing = False
        self.status = "stopped"
        self.setup_running = False
        self.setup_timer = None
        self.empty_labels = {}
        self.colors = palette_for(settings.theme)
        self.current_page = "主界面"
        self.active_conversation = None
        self.local_api_requests = {}
        root.title("NapCat 适配器")
        root.geometry("1440x900")
        root.minsize(1120, 740)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style = apply_theme(root, settings.theme)
        root.bind("<Map>", lambda event: style_titlebar(root, self.settings.theme) if event.widget is root else None, add="+")
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        nav = tk.Frame(shell, bg=self.colors["sidebar"], width=194)
        self.sidebar = nav
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self.sidebar_labels = []
        title = tk.Label(nav, text="NapCat", bg=self.colors["sidebar"], fg=self.colors["ink"], font=(FONT, 15, "bold"))
        title.pack(anchor="w", padx=22, pady=(27, 2))
        self.sidebar_labels.append((title, "ink"))
        subtitle = tk.Label(nav, text="消息适配器", bg=self.colors["sidebar"], fg=self.colors["muted"], font=(FONT, 9))
        subtitle.pack(anchor="w", padx=23, pady=(0, 32))
        self.sidebar_labels.append((subtitle, "muted"))
        caption = tk.Label(nav, text="工作区", bg=self.colors["sidebar"], fg=self.colors["muted"], font=(FONT, 9))
        caption.pack(anchor="w", padx=23, pady=(0, 8))
        self.sidebar_labels.append((caption, "muted"))
        self.sidebar_border = tk.Frame(shell, bg=self.colors["line"], width=1)
        self.sidebar_border.pack(side="left", fill="y")
        body = ttk.Frame(shell, padding=(26, 20, 26, 24))
        body.pack(side="left", fill="both", expand=True)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)
        topbar = ttk.Frame(body, style="Header.TFrame", padding=(0, 9))
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        self.status_label = ttk.Label(topbar, style="Header.TLabel", font=(FONT, 9))
        self.status_label.pack(side="left")
        ttk.Label(topbar, text="LCC AGENT  /  QQ", style="Header.TLabel", font=(FONT, 9)).pack(side="right")
        self.frames, self.nav_buttons = {}, {}
        for name in ("主界面", "聊天配置", "NapCat配置"):
            frame = ttk.Frame(body)
            frame.grid(row=1, column=0, sticky="nsew")
            self.frames[name] = frame
            button = tk.Button(nav, text=name, command=lambda n=name: self.show(n),
                               relief="flat", borderwidth=0, anchor="w", padx=12, pady=10,
                               font=(FONT, 10),
                               cursor="hand2", bg=self.colors["sidebar"], fg=self.colors["muted"],
                               activebackground=self.colors["hover"], activeforeground=self.colors["ink"])
            button.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[name] = button
        self.appearance = tk.Frame(nav, bg=self.colors["sidebar"])
        self.appearance.pack(side="bottom", fill="x", padx=18, pady=24)
        appearance_label = tk.Label(self.appearance, text="外观", bg=self.colors["sidebar"], fg=self.colors["muted"], font=(FONT, 9))
        appearance_label.pack(anchor="w", padx=4, pady=(0, 9))
        self.sidebar_labels.append((appearance_label, "muted"))
        self.theme_buttons = {}
        for name, label in (("light", "浅色"), ("dark", "深色")):
            button = ttk.Button(self.appearance, text=label, width=6, style="SidebarChoice.TButton",
                                command=lambda selected=name: self.change_theme(selected))
            button.pack(side="left", padx=(0, 6))
            self.theme_buttons[name] = button
        self._build_home(self.frames["主界面"])
        self._build_chats(self.frames["聊天配置"])
        self._build_connection(self.frames["NapCat配置"], token)
        self.log_handler = QueueLog(self.events)
        logging.getLogger().addHandler(self.log_handler)
        self._paint_theme()
        self.show("主界面")
        self.refresh()
        self.timer = root.after(80, self.pump)
        if auto_connect:
            self.connect()

    def emit_event(self, event):
        if event.get("type") == "send_result":
            self.console_api_event(event)
        self._external_emit(event)

    def show(self, name):
        self.current_page = name
        self.frames[name].tkraise()
        for key, button in self.nav_buttons.items():
            button.configure(bg=self.colors["selected"] if key == name else self.colors["sidebar"],
                             fg=self.colors["ink"] if key == name else self.colors["muted"])

    def change_theme(self, name):
        colors = palette_for(name)
        if self.settings.theme == name:
            return
        self.settings.theme = name
        self.colors = colors
        self.style = apply_theme(self.root, name)
        self._paint_theme()
        self.persist()

    def _paint_theme(self):
        """原地更新 Tk 控件和表格标签，保留内容、选中项、滚动位置与运行状态。"""
        colors = self.colors
        self.sidebar.configure(bg=colors["sidebar"])
        self.appearance.configure(bg=colors["sidebar"])
        self.sidebar_border.configure(bg=colors["line"])
        for widget, role in self.sidebar_labels:
            widget.configure(bg=colors["sidebar"], fg=colors[role])
        for button in self.nav_buttons.values():
            button.configure(activebackground=colors["hover"], activeforeground=colors["ink"])
        for name, button in self.theme_buttons.items():
            button.configure(style="SidebarSelected.TButton" if name == self.settings.theme else "SidebarChoice.TButton")
        for tree in (*self.trees.values(), self.chats, self.home_chats):
            self._paint_tree(tree)
        for widget in self.details.values():
            widget.configure(bg=colors["input"], fg=colors["ink"], insertbackground=colors["ink"],
                             selectbackground=colors["selected"], selectforeground=colors["selected_ink"])
        self.log.configure(bg=colors["log_bg"], fg=colors["log_ink"], insertbackground=colors["ink"],
                           selectbackground=colors["selected"], selectforeground=colors["selected_ink"])
        self.status_label.configure(foreground=colors["green"] if self.status == "connected" else colors["muted"])
        self.show(self.current_page)
        self._paint_comboboxes(self.root)

    def _paint_tree(self, tree):
        colors = self.colors
        tree.tag_configure("odd", background=colors["odd"])
        for tag, color in (("pending", "amber"), ("ok", "green"), ("not_sent", "muted"),
                           ("failed", "red"), ("unknown", "red")):
            tree.tag_configure(tag, foreground=colors[color])

    def _paint_comboboxes(self, widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Combobox):
                # 已打开过的下拉列表已存在，不能只更新 option database。
                popdown = self.root.tk.call("ttk::combobox::PopdownWindow", str(child))
                self.root.tk.call(str(popdown) + ".f.l", "configure", "-background", self.colors["card"],
                                  "-foreground", self.colors["ink"], "-selectbackground", self.colors["selected"],
                                  "-selectforeground", self.colors["selected_ink"])
            self._paint_comboboxes(child)

    def heading(self, frame, title, hint):
        ttk.Label(frame, text=title, style="Title.TLabel").pack(anchor="w")
        label = ttk.Label(frame, text=hint, style="Hint.TLabel", wraplength=940)
        label.pack(anchor="w", pady=(7, 22))
        frame.bind("<Configure>", lambda event: label.configure(wraplength=max(320, event.width - 10)), add="+")

    def table(self, parent, columns, widths):
        frame = ttk.Frame(parent, style="CardBody.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, height=5, show="headings", selectmode="extended")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._paint_tree(tree)
        tree.bind("<Control-a>", lambda _: self.select_rows(tree, True))
        tree.bind("<Escape>", lambda _: self.select_rows(tree, False))
        empty = ttk.Label(tree, text="暂无记录", style="CardHint.TLabel", justify="center")
        self.empty_labels[tree] = empty
        for column, width in zip(columns, widths):
            tree.heading(column, text=column)
            tree.column(column, width=width, minwidth=55, stretch=column == columns[-1])
        return tree

    def _build_home(self, frame):
        self.heading(frame, "消息工作台", "选择左侧已启用的聊天，分别查看收发消息。手动 / 自动模式仍适用于所有启用聊天。")
        self.active_chat_label = ttk.Label(frame, text="尚未启用聊天", style="Hint.TLabel")
        self.active_chat_label.pack(anchor="w", pady=(0, 12))
        metrics = ttk.Frame(frame)
        metrics.pack(fill="x", pady=(0, 20))
        self.metrics = {}
        for index, (key, title, hint) in enumerate((("receive", "待接收", "当前聊天 · 等待交给 Agent"),
                                                  ("send", "待发送", "当前聊天 · 等待交给 NapCat"),
                                                  ("chats", "已启用会话", "群聊与私聊"))):
            metrics.columnconfigure(index, weight=1, uniform="metric")
            card = ttk.Frame(metrics, style="Card.TFrame", padding=(18, 10))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0 if index == 2 else 7))
            ttk.Label(card, text=title, style="CardHint.TLabel").pack(anchor="w")
            value = ttk.Label(card, text="0", style="Metric.TLabel")
            value.pack(anchor="w", pady=(1, 2))
            ttk.Label(card, text=hint, style="CardHint.TLabel", font=(FONT, 9)).pack(anchor="w")
            self.metrics[key] = value
        workspace = ttk.Frame(frame)
        workspace.pack(fill="both", expand=True)
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, minsize=192)
        workspace.columnconfigure(1, weight=1)
        chat_panel = ttk.Frame(workspace, width=192, style="Card.TFrame", padding=12)
        chat_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        chat_panel.grid_propagate(False)
        chat_panel.columnconfigure(0, weight=1)
        chat_panel.rowconfigure(2, weight=1)
        ttk.Label(chat_panel, text="已启用聊天", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.home_chat_type = tk.StringVar(value="全部")
        self.home_chat_filter = ttk.Combobox(chat_panel, textvariable=self.home_chat_type,
                                            values=("全部", "群聊", "私聊", "完整接口"), state="readonly", width=8)
        self.home_chat_filter.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.home_chat_filter.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        list_frame = ttk.Frame(chat_panel, style="CardBody.TFrame")
        list_frame.grid(row=2, column=0, sticky="nsew")
        self.home_chats = ttk.Treeview(list_frame, columns=("pending",), show="tree headings", selectmode="browse", height=5)
        self.home_chats.heading("#0", text="会话")
        self.home_chats.column("#0", width=112, minwidth=65, stretch=True)
        self.home_chats.heading("pending", text="收 / 发")
        self.home_chats.column("pending", width=48, minwidth=42, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.home_chats.yview)
        self.home_chats.configure(yscrollcommand=scroll.set)
        self.home_chats.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.home_chats.bind("<<TreeviewSelect>>", self.select_home_chat)
        self.empty_labels[self.home_chats] = ttk.Label(self.home_chats, text="暂无启用聊天\n请在聊天配置中开启", justify="center", style="CardHint.TLabel", wraplength=135)
        ttk.Label(chat_panel, text="待办：接收 / 发送", style="CardHint.TLabel", font=(FONT, 9)).grid(row=3, column=0, sticky="w", pady=(10, 0))
        panes = ttk.Panedwindow(workspace, orient="horizontal")
        panes.grid(row=0, column=1, sticky="nsew")
        self.trees, self.modes, self.counts, self.details = {}, {}, {}, {}
        for direction, title, action in (("receive", "接收列表", "接收选中"), ("send", "发送列表", "发送选中")):
            section = ttk.Frame(panes, style="Card.TFrame", padding=12)
            panes.add(section, weight=1)
            ttk.Label(section, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
            controls = ttk.Frame(section, style="CardBody.TFrame")
            controls.pack(fill="x", pady=(0, 10))
            self.modes[direction] = tk.StringVar(value=getattr(self.settings, direction + "_mode"))
            for value, label in (("manual", "手动"), ("auto", "自动")):
                ttk.Radiobutton(controls, text=label, value=value, style="Mode.TRadiobutton", variable=self.modes[direction],
                                command=lambda d=direction: self.change_mode(d)).pack(side="left", padx=(0, 10))
            count = ttk.Label(controls, style="CardHint.TLabel", font=(FONT, 9))
            count.pack(side="right")
            self.counts[direction] = count
            selection = ttk.Frame(section, style="CardBody.TFrame")
            selection.pack(fill="x", pady=(0, 10))
            ttk.Label(selection, text="选择消息", style="CardHint.TLabel", font=(FONT, 9)).pack(side="left")
            ttk.Button(selection, text="全不选", style="Small.TButton", command=lambda d=direction: self.select_rows(self.trees[d], False)).pack(side="right")
            ttk.Button(selection, text="全选", style="Small.TButton", command=lambda d=direction: self.select_rows(self.trees[d], True)).pack(side="right", padx=6)
            tree = self.table(section, ("状态", "会话", "消息"), (90, 85, 220))
            self.trees[direction] = tree
            tree.bind("<<TreeviewSelect>>", lambda event, d=direction: self.show_detail(d))
            self.empty_labels[tree].configure(text="暂无消息\n启用会话后，消息将显示在这里")
            buttons = ttk.Frame(section, style="CardBody.TFrame")
            buttons.pack(fill="x", pady=10)
            ttk.Button(buttons, text=action, style="Primary.TButton", command=lambda d=direction: self.release(d)).pack(side="left")
            ttk.Button(buttons, text="删除选中", style="Danger.TButton", command=lambda d=direction: self.delete(d)).pack(side="left", padx=8)
            ttk.Label(section, text="消息详情", style="CardHint.TLabel", font=(FONT, 9)).pack(anchor="w", pady=(3, 7))
            detail = tk.Text(section, height=4, wrap="word", relief="flat", borderwidth=0, padx=12, pady=10,
                             bg=self.colors["input"], fg=self.colors["ink"], selectbackground=self.colors["selected"], font=(FONT, 10))
            detail.pack(fill="x")
            detail.configure(state="disabled")
            self.details[direction] = detail
            # 固定操作栏和详情区，只让中间消息表格伸缩，避免小窗口挤掉按钮。
            widgets = section.winfo_children()
            for widget in widgets:
                widget.pack_forget()
            for row, widget in enumerate(widgets):
                widget.grid(row=row, column=0, sticky="nsew" if row == 3 else "ew",
                            pady=(0, 9) if row in (0, 1, 2, 3, 4) else (0, 0))
            section.columnconfigure(0, weight=1)
            section.rowconfigure(3, weight=1)

    def _build_chats(self, frame):
        self.heading(frame, "聊天配置", "仅让指定会话参与收发。所有新会话默认关闭，关闭会话会取消尚未放行的消息。")
        card = ttk.Frame(frame, style="Card.TFrame", padding=18)
        card.pack(fill="both", expand=True, pady=(0, 18))
        controls = ttk.Frame(card, style="CardBody.TFrame")
        controls.pack(fill="x", pady=(0, 12))
        self.search = tk.StringVar()
        self.chat_type = tk.StringVar(value="全部")
        ttk.Entry(controls, textvariable=self.search, width=28).pack(side="left")
        self.search.trace_add("write", lambda *_: self.refresh_chats())
        box = ttk.Combobox(controls, textvariable=self.chat_type, values=("全部", "群聊", "私聊", "完整接口"), state="readonly", width=8)
        box.pack(side="left", padx=8)
        box.bind("<<ComboboxSelected>>", lambda _: self.refresh_chats())
        ttk.Button(controls, text="刷新群聊 / 好友", command=self.network.catalog).pack(side="right")
        self.chats = self.table(card, ("启用", "类型", "QQ / 群号", "名称"), (90, 90, 180, 400))
        self.empty_labels[self.chats].configure(text="还没有会话\n连接 NapCat 后刷新，或在下方手动添加")
        self.chats.bind("<Double-1>", self.toggle_chat)
        controls = ttk.Frame(card, style="CardBody.TFrame")
        controls.pack(fill="x", pady=(14, 0))
        ttk.Button(controls, text="启用选中", style="Primary.TButton", command=lambda: self.enable_chats(True)).pack(side="left")
        ttk.Button(controls, text="关闭选中", command=lambda: self.enable_chats(False)).pack(side="left", padx=8)
        ttk.Button(controls, text="全不选", command=lambda: self.select_rows(self.chats, False)).pack(side="right")
        ttk.Button(controls, text="全选", command=lambda: self.select_rows(self.chats, True)).pack(side="right", padx=8)
        self.chat_selection = ttk.Label(card, style="CardHint.TLabel", font=(FONT, 9))
        self.chat_selection.pack(anchor="w", pady=(10, 0))
        self.chats.bind("<<TreeviewSelect>>", lambda _: self.update_chat_selection())
        add = ttk.LabelFrame(frame, text="手动添加会话", padding=16, style="Card.TLabelframe")
        add.pack(fill="x")
        self.add_type, self.add_id, self.add_name = tk.StringVar(value="群聊"), tk.StringVar(), tk.StringVar()
        ttk.Combobox(add, values=("群聊", "私聊"), textvariable=self.add_type, state="readonly", width=8).pack(side="left")
        ttk.Label(add, text=" 号码 ", style="Card.TLabel").pack(side="left")
        ttk.Entry(add, textvariable=self.add_id, width=19).pack(side="left")
        ttk.Label(add, text=" 名称 ", style="Card.TLabel").pack(side="left")
        ttk.Entry(add, textvariable=self.add_name, width=20).pack(side="left")
        ttk.Button(add, text="添加（默认关闭）", command=self.add_chat).pack(side="left", padx=10)

    def _build_connection(self, frame, token):
        self.heading(frame, "NapCat 配置", "配置本机 NapCat 服务并连接，或手动填写已有 WebSocket 服务的参数。")
        upper = ttk.Frame(frame)
        upper.pack(fill="x", pady=(0, 20))
        upper.columnconfigure(0, weight=4, uniform="config")
        upper.columnconfigure(1, weight=6, uniform="config")
        setup = ttk.Frame(upper, style="Card.TFrame", padding=18)
        setup.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(setup, text="一键接入本机 NapCat", style="CardTitle.TLabel").pack(anchor="w")
        caption = ttk.Label(setup, text="自动匹配连接参数，需要时备份并添加专用\nWebSocket 服务，保留其他服务设置。", style="CardHint.TLabel", justify="left")
        caption.pack(anchor="w", pady=(8, 16))
        ttk.Label(setup, text="NapCat 配置文件", style="CardHint.TLabel", font=(FONT, 9)).pack(anchor="w", pady=(0, 7))
        self.napcat_path = tk.StringVar(value=self.settings.napcat_config_path)
        ttk.Entry(setup, textvariable=self.napcat_path).pack(fill="x")
        actions = ttk.Frame(setup, style="CardBody.TFrame")
        actions.pack(fill="x", pady=(10, 12))
        ttk.Button(actions, text="选择文件", command=self.choose_napcat_file).pack(side="left")
        self.setup_button = ttk.Button(actions, text="一键自动配置", style="Primary.TButton", command=self.auto_configure)
        self.setup_button.pack(side="left", padx=(8, 0))
        self.setup_status = tk.StringVar(value="首次找不到配置时，请选择 onebot11_账号.json。")
        status = ttk.Label(setup, textvariable=self.setup_status, style="CardHint.TLabel", justify="left", wraplength=310)
        status.pack(anchor="w", fill="x")
        setup.bind("<Configure>", lambda event: (caption.configure(wraplength=max(180, event.width - 38)),
                                                status.configure(wraplength=max(180, event.width - 38))))
        manual = ttk.Frame(upper, style="Card.TFrame", padding=18)
        manual.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        ttk.Label(manual, text="连接参数", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        form = ttk.Frame(manual, style="CardBody.TFrame")
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.connection_vars = {}
        fields = (("url", "WebSocket 地址"), ("token", "Access Token"),
                  ("api_timeout", "接口超时（秒）"), ("reconnect_delay", "重连间隔（秒）"))
        for index, (key, label) in enumerate(fields):
            ttk.Label(form, text=label, style="Card.TLabel").grid(row=index, column=0, sticky="w", padx=(0, 14), pady=5)
            value = token if key == "token" else self.settings.connection[key]
            variable = tk.StringVar(value=str(value))
            self.connection_vars[key] = variable
            ttk.Entry(form, textvariable=variable, width=24, show="•" if key == "token" else "").grid(row=index, column=1, sticky="ew", pady=5)
        self.require_at = tk.BooleanVar(value=self.settings.connection["require_at"])
        ttk.Label(form, text="接收全部事件；按聊天配置启用和放行。", style="CardHint.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        controls = ttk.Frame(manual, style="CardBody.TFrame")
        controls.pack(fill="x", pady=(12, 0))
        ttk.Button(controls, text="保存配置", command=self.save_connection).pack(side="left")
        ttk.Button(controls, text="连接", style="Primary.TButton", command=self.connect).pack(side="left", padx=8)
        ttk.Button(controls, text="断开", command=self.network.stop).pack(side="left")
        ttk.Button(manual, text="打开通用 API 控制台", command=self.open_api_console).pack(anchor="w", pady=(10, 0))
        logs = ttk.Frame(frame, style="Card.TFrame", padding=18)
        logs.pack(fill="both", expand=True)
        ttk.Label(logs, text="运行日志", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        log_body = ttk.Frame(logs, style="CardBody.TFrame")
        log_body.pack(fill="both", expand=True)
        self.log = tk.Text(log_body, height=5, wrap="word", bg=self.colors["log_bg"], fg=self.colors["log_ink"], state="disabled",
                           relief="flat", borderwidth=0, padx=14, pady=12, font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(log_body, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def open_api_console(self):
        """适配器自己的操作入口；不需要 Agent 注册或调用通用工具。"""
        window = tk.Toplevel(self.root)
        window.title("NapCat 通用 API")
        window.geometry("780x650")
        window.minsize(600, 480)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="使用 NapCat 原生 action 和 JSON 参数；在聊天配置中启用“全部事件 / 通用 API”。").pack(anchor="w")
        ttk.Label(frame, text="请求进入主界面的发送队列，沿用人工 / 自动放行设置。").pack(anchor="w", pady=(3, 12))
        form = ttk.Frame(frame)
        form.pack(fill="x")
        action = tk.StringVar(value="get_login_info")
        timeout = tk.StringVar(value=str(self.settings.connection["api_timeout"]))
        stream = tk.StringVar(value="自动")
        ttk.Label(form, text="action").pack(side="left")
        ttk.Entry(form, textvariable=action).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(form, text="超时（秒）").pack(side="left")
        ttk.Entry(form, textvariable=timeout, width=7).pack(side="left", padx=8)
        ttk.Combobox(form, textvariable=stream, values=("自动", "单帧", "多帧"), state="readonly", width=6).pack(side="left")
        ttk.Label(frame, text="params（JSON 对象）").pack(anchor="w", pady=(12, 4))
        params = tk.Text(frame, height=7, wrap="word")
        params.pack(fill="x")
        params.insert("1.0", "{}")
        path = tk.StringVar()
        files = ttk.Frame(frame)
        files.pack(fill="x", pady=10)
        ttk.Button(files, text="保存完整响应为 JSONL…", command=lambda: path.set(
            filedialog.asksaveasfilename(parent=window, defaultextension=".jsonl", filetypes=(("JSONL", "*.jsonl"),)) or path.get())).pack(side="left")
        ttk.Entry(files, textvariable=path).pack(side="left", fill="x", expand=True, padx=8)
        output = tk.Text(frame, height=10, wrap="word", state="disabled")
        output.pack(fill="both", expand=True)
        status = tk.StringVar(value="就绪；大文件下载建议选择 JSONL 文件，保留所有 Base64 分块。")
        ttk.Label(frame, textvariable=status, wraplength=720).pack(anchor="w", pady=8)
        submit = ttk.Button(frame, text="加入发送队列", style="Primary.TButton")
        submit.pack(anchor="e")

        def enqueue():
            command = dict(type="call_api", request_id=uuid.uuid4().hex,
                           conversation_id=SYSTEM_CONVERSATION, action=action.get(), params={})
            stream_file = None
            try:
                if not self.model.enabled(SYSTEM_CONVERSATION):
                    raise ValueError("请先在聊天配置中启用“全部事件 / 通用 API”")
                command.update(params=json.loads(params.get("1.0", "end")), timeout=float(timeout.get()),
                               stream={"自动": None, "单帧": False, "多帧": True}[stream.get()])
                validate_send_command(command)
                if path.get().strip():
                    # 独占创建，避免控制台覆盖已有结果文件。
                    stream_file = open(path.get().strip(), "x", encoding="utf-8")
            except (ValueError, OSError) as exc:
                status.set(str(exc))
                return
            record = dict(output=output, status=status, button=submit, file=stream_file, displayed=0, omitted=False)
            self.local_api_requests[command["request_id"]] = record
            submit.configure(state="disabled")
            status.set("已加入发送队列；手动模式请在主界面选择完整接口并放行。")
            self.model.enqueue(command)
            if command["request_id"] in self.model.results:
                self.console_api_event(self.model.results[command["request_id"]])
            self.refresh()
        submit.configure(command=enqueue)
        return window

    def console_api_event(self, event):
        record = self.local_api_requests.get(event.get("request_id"))
        if record is None:
            return
        final = event["type"] == "send_result"
        value = event.get("response", event)
        line = json.dumps(value, ensure_ascii=False) + "\n"
        if record["file"] is not None:
            try:
                record["file"].write(line)
                record["file"].flush()
            except OSError as exc:
                record["file"].close()
                record["file"] = None
                record["file_error"] = type(exc).__name__
        if record["output"].winfo_exists():
            widget = record["output"]
            widget.configure(state="normal")
            # 文件保留全部帧；窗口限制预览大小，避免大文件阻塞界面。
            available = max(0, 100000 - record["displayed"])
            widget.insert("end", line[:available])
            record["displayed"] += min(len(line), available)
            if len(line) > available and not record["omitted"]:
                widget.insert("end", "\n[后续内容超出预览上限，请查看所选 JSONL 文件]\n")
                record["omitted"] = True
            widget.configure(state="disabled")
            widget.see("end")
            if final:
                record["button"].configure(state="normal")
                record["status"].set(event.get("error") or event.get("status", "unknown"))
                if record.get("file_error"):
                    record["status"].set("结果文件写入失败：" + record["file_error"])
        if final:
            if record["file"] is not None:
                record["file"].close()
            self.local_api_requests.pop(event.get("request_id"), None)

    def select_rows(self, tree, select):
        """仅选择当前列表中的行，不触发放行、发送或开启会话。"""
        if select:
            tree.selection_set(tree.get_children())
        else:
            tree.selection_remove(tree.selection())
        if tree is self.chats:
            self.update_chat_selection()
        else:
            for direction, candidate in self.trees.items():
                if candidate is tree:
                    self.show_detail(direction)
        return "break"

    def update_chat_selection(self):
        self.chat_selection.configure(text=f"显示 {len(self.chats.get_children())} 个会话 · 已选择 {len(self.chats.selection())} 个（全选仅作用于当前筛选结果）")

    def show_empty(self, tree):
        label = self.empty_labels[tree]
        if tree.get_children():
            label.place_forget()
        else:
            label.place(relx=0.5, rely=0.5, anchor="center")

    def choose_napcat_file(self, initialdir=None):
        current = self.napcat_path.get().strip()
        directory = initialdir or (str(Path(current).parent) if current else str(Path.home()))
        selected = filedialog.askopenfilename(parent=self.root, title="选择 NapCat 的 OneBot 配置（多账号请选对应账号）",
                                              initialdir=directory,
                                              filetypes=(("NapCat OneBot 配置", "onebot11*.json"), ("JSON", "*.json")))
        if selected:
            self.napcat_path.set(selected)
            self.setup_status.set("配置文件已选择，点击一键自动配置即可应用。")
        return selected

    def auto_configure(self):
        if self.setup_running or self.closing:
            return
        path = self.napcat_path.get().strip()
        if not path:
            found = find_configs(self.settings.napcat_config_path)
            if len(found) == 1:
                path = str(found[0])
                self.napcat_path.set(path)
            else:
                path = self.choose_napcat_file(str(found[0].parent) if found else None)
                if not path:
                    return
        self.setup_running = True
        self.setup_button.configure(state="disabled")
        self.setup_status.set("正在检查并配置本机 NapCat…")
        def worker():
            try:
                result = configure_local(path)
                self.events.put(dict(type="auto_configured", result=result))
            except (ValueError, OSError) as exc:
                self.events.put(dict(type="auto_config_failed", error=str(exc)))
            except Exception as exc:
                self.events.put(dict(type="auto_config_failed", error=f"自动配置失败：{type(exc).__name__}"))
        threading.Thread(target=worker, name="napcat-auto-config", daemon=True).start()

    def apply_auto_config(self, result):
        self.setup_running = False
        self.settings.napcat_config_path = str(result.path)
        self.napcat_path.set(str(result.path))
        self.connection_vars["url"].set(result.url)
        self.connection_vars["token"].set(result.token)
        # 保留用户的消息策略；仅修正无效的超时输入，保证一键流程可完成。
        for key, default in (("api_timeout", "15"), ("reconnect_delay", "3")):
            try:
                value = float(self.connection_vars[key].get())
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("无效超时")
            except ValueError:
                self.connection_vars[key].set(default)
        if not self.save_connection():
            self.setup_button.configure(state="normal")
            self.setup_status.set("NapCat 已处理，但适配器设置保存失败，请检查目录权限。")
            return
        if result.changed:
            self.setup_status.set("配置已写入并备份。请在 NapCat WebUI 保存配置或重启 NapCat，适配器将自动重连。")
            self.append_log("NapCat 配置已更新，备份：" + str(result.backup))
        else:
            self.setup_status.set("已读取现有服务配置，正在连接；若仍失败，请在 NapCat WebUI 保存配置或重启 NapCat。")
            self.append_log("已复用 NapCat 现有 WebSocket 服务。")
        if (self.status == "connected" and self.network.adapter is not None and
                self.network.adapter.url == result.url and self.network.adapter.token == result.token):
            self.setup_button.configure(state="normal")
            return
        self.network.stop()
        deadline = time.monotonic() + 5
        def reconnect():
            self.setup_timer = None
            if self.closing:
                return
            if self.network.thread and self.network.thread.is_alive():
                if time.monotonic() < deadline:
                    self.setup_timer = self.root.after(75, reconnect)
                    return
                self.setup_status.set("配置已保存，当前连接尚未停止，请稍后手动连接。")
            else:
                self.connect()
            self.setup_button.configure(state="normal")
        reconnect()

    def persist(self):
        try:
            self.settings.save()
            return True
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False

    def save_connection(self):
        try:
            connection = {key: self.connection_vars[key].get().strip() for key in ("url", "api_timeout", "reconnect_delay")}
            for key in ("api_timeout", "reconnect_delay"):
                connection[key] = float(connection[key])
            connection["require_at"] = self.require_at.get()
            Settings.validate_connection(connection)
            self.settings.connection = connection
            if self.persist():
                self.append_log("配置已保存；连接参数在下次连接时生效。")
                return True
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc), parent=self.root)
        return False

    def connect(self):
        if self.closing or not self.save_connection():
            return
        try:
            self.network.start(dict(self.settings.connection), self.connection_vars["token"].get())
            self.status = "connecting"
        except ValueError as exc:
            self.append_log(str(exc))
        self.refresh()

    def change_mode(self, direction):
        self.model.set_mode(direction, self.modes[direction].get())
        self.persist()
        self.refresh()

    def release(self, direction):
        keys = self.selected_message_keys(direction)
        (self.model.release_incoming if direction == "receive" else self.model.release_outgoing)(keys)
        self.refresh()

    def delete(self, direction):
        self.model.delete(direction, self.selected_message_keys(direction))
        self.refresh()

    def enable_chats(self, enabled):
        self.model.set_enabled(self.chats.selection(), enabled)
        self.persist()
        self.refresh()

    def toggle_chat(self, event):
        key = self.chats.identify_row(event.y)
        if key:
            self.model.set_enabled([key], not self.model.enabled(key))
            self.persist()
            self.refresh()

    def add_chat(self):
        try:
            kind = "group" if self.add_type.get() == "群聊" else "private"
            self.model.discover(f"qq:{kind}:{self.add_id.get().strip()}", self.add_name.get().strip())
            self.persist()
            self.refresh()
        except ValueError as exc:
            messagebox.showerror("号码无效", str(exc), parent=self.root)

    def chat_label(self, conversation):
        return self.settings.sessions.get(conversation, {}).get("name") or conversation.split(":")[-1]

    def visible_messages(self, direction):
        """仅返回当前已启用聊天的记录；显示筛选不改变真实收发队列。"""
        rows = self.model.incoming if direction == "receive" else self.model.outgoing
        conversation = self.active_conversation
        if conversation is None or not self.model.enabled(conversation):
            return {}
        return {key: row for key, row in rows.items() if row["event"]["conversation_id"] == conversation}

    def selected_message_keys(self, direction):
        visible = self.visible_messages(direction)
        return [key for key in self.trees[direction].selection() if key in visible]

    def select_home_chat(self, event=None):
        selected = self.home_chats.selection()
        if not selected:
            return
        conversation = selected[0]
        if not self.model.enabled(conversation):
            self.refresh()
            return
        if conversation != self.active_conversation:
            self.active_conversation = conversation
            self.refresh()

    def refresh_home_chats(self):
        kind = self.home_chat_type.get()
        enabled = []
        for conversation, record in self.settings.sessions.items():
            if not record["enabled"]:
                continue
            label = self.session_kind(conversation)
            if kind != "全部" and kind != label:
                continue
            enabled.append((conversation, record, label))
        enabled.sort(key=lambda item: (item[2] != "群聊", (item[1]["name"] or item[0]).casefold(), item[0]))
        counts = {conversation: [0, 0] for conversation, _, _ in enabled}
        for index, rows in enumerate((self.model.incoming, self.model.outgoing)):
            for row in rows.values():
                conversation = row["event"]["conversation_id"]
                if row["state"] == "pending" and conversation in counts:
                    counts[conversation][index] += 1
        keys = {conversation for conversation, _, _ in enabled}
        for key in set(self.home_chats.get_children()) - keys:
            self.home_chats.delete(key)
        for index, (conversation, record, label) in enumerate(enabled):
            pending = counts[conversation]
            tags = (("odd",) if index % 2 else ()) + (("pending",) if any(pending) else ())
            text = f"{label} · {self.chat_label(conversation)}"
            values = (f"{pending[0]} / {pending[1]}",)
            if self.home_chats.exists(conversation):
                self.home_chats.item(conversation, text=text, values=values, tags=tags)
                if self.home_chats.index(conversation) != index:
                    self.home_chats.move(conversation, "", index)
            else:
                self.home_chats.insert("", index, iid=conversation, text=text, values=values, tags=tags)
        if self.active_conversation not in keys:
            self.active_conversation = enabled[0][0] if enabled else None
        expected_selection = (self.active_conversation,) if self.active_conversation else ()
        if self.home_chats.selection() != expected_selection:
            if expected_selection:
                self.home_chats.selection_set(expected_selection)
                self.home_chats.see(self.active_conversation)
            else:
                self.home_chats.selection_remove(self.home_chats.selection())
        if self.active_conversation:
            kind_label = self.session_kind(self.active_conversation)
            self.active_chat_label.configure(text=f"{self.chat_label(self.active_conversation)}  ·  {kind_label}  {self.active_conversation.split(':')[-1]}")
        else:
            self.active_chat_label.configure(text="尚未启用聊天，请先在聊天配置中开启" if not any(item["enabled"] for item in self.settings.sessions.values()) else "当前分类没有已启用聊天")
        self.empty_labels[self.home_chats].configure(text="暂无启用聊天\n请在聊天配置中开启" if not any(item["enabled"] for item in self.settings.sessions.values()) else "当前分类暂无聊天")
        self.show_empty(self.home_chats)

    def refresh_chats(self):
        selected = self.chats.selection()
        self.chats.delete(*self.chats.get_children())
        search, kind = self.search.get().casefold(), self.chat_type.get()
        for index, (key, record) in enumerate(sorted(self.settings.sessions.items())):
            label = self.session_kind(key)
            if kind != "全部" and kind != label:
                continue
            if search not in (key + record["name"]).casefold():
                continue
            self.chats.insert("", "end", iid=key, values=("● 已开启" if record["enabled"] else "○ 关闭", label, key.split(":")[-1], record["name"]),
                              tags=(("odd",) if index % 2 else ()) + (("ok",) if record["enabled"] else ()))
        self.chats.selection_set([key for key in selected if self.chats.exists(key)])
        self.show_empty(self.chats)
        self.update_chat_selection()

    @staticmethod
    def session_kind(conversation):
        return "完整接口" if ":system:" in conversation else "群聊" if ":group:" in conversation else "私聊"

    def refresh(self):
        self.refresh_home_chats()
        labels = dict(stopped="已停止", connected="已连接", disconnected="断线，等待重连", connecting="连接中")
        enabled = sum(item["enabled"] for item in self.settings.sessions.values())
        self.status_label.configure(text=f"●  {labels.get(self.status, self.status)}    /    {enabled} 个会话已启用    /    丢弃 {self.model.dropped} 条",
                                    foreground=self.colors["green"] if self.status == "connected" else self.colors["muted"])
        self.metrics["chats"].configure(text=str(enabled))
        for direction in ("receive", "send"):
            rows = self.visible_messages(direction)
            tree = self.trees[direction]
            existing = set(tree.get_children())
            for key in existing - rows.keys():
                tree.delete(key)
            for index, (key, row) in enumerate(rows.items()):
                event = row["event"]
                values = (self.STATES[row["state"]], self.chat_label(event["conversation_id"]), event["text"].replace("\n", " ⏎ "))
                tags = (("odd",) if index % 2 else ()) + (row["state"],)
                if key in existing:
                    tree.item(key, values=values, tags=tags)
                else:
                    tree.insert("", "end", iid=key, values=values, tags=tags)
            pending = sum(row["state"] == "pending" for row in rows.values())
            self.counts[direction].configure(text=f"待办 {pending} / 共 {len(rows)}")
            self.metrics[direction].configure(text=str(pending))
            self.empty_labels[tree].configure(text="当前聊天暂无消息" if self.active_conversation else "请先选择已启用的聊天")
            self.show_empty(tree)
            self.show_detail(direction)
        self.refresh_chats()

    def show_detail(self, direction):
        selected = self.trees[direction].selection()
        rows = self.visible_messages(direction)
        pending = sum(row["state"] == "pending" for row in rows.values())
        self.counts[direction].configure(text=f"待办 {pending} · 已选 {len(selected)} / {len(rows)}")
        text = "选中消息查看完整内容。"
        if selected and selected[0] in rows:
            row = rows[selected[0]]
            event = row["event"]
            sender = event.get("metadata", {}).get("sender_name") or event.get("sender_id", "Agent")
            text = f"{event['conversation_id']} · {sender}\n{event['text']}"
            if direction == "receive" and "raw_event" in event:
                text += "\n\nNapCat 原始事件：\n" + json.dumps(
                    event["raw_event"], ensure_ascii=False, indent=2)
            if direction == "send" and "params" in event:
                text += "\n\nNapCat 发送参数：\n" + json.dumps(
                    event["params"], ensure_ascii=False, indent=2)
            if row.get("result", {}).get("error"):
                text += "\n\n" + row["result"]["error"]
            if "raw_response" in row.get("result", {}):
                text += "\n\nNapCat 完整回执：\n" + json.dumps(
                    row["result"]["raw_response"], ensure_ascii=False, indent=2)
        widget = self.details[direction]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def append_log(self, text):
        for token in (os.getenv("NAPCAT_TOKEN", ""), self.connection_vars["token"].get()):
            if token:
                text = text.replace(token, "[Token 已隐藏]")
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 600:
            self.log.delete("1.0", f"{lines - 600}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        dirty = False
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type") if isinstance(event, dict) else None
            if kind == "shutdown":
                self.close()
                return
            if kind == "command":
                self.model.enqueue(event["command"])
            elif kind == "auto_configured":
                self.apply_auto_config(event["result"])
            elif kind == "auto_config_failed":
                self.setup_running = False
                self.setup_button.configure(state="normal")
                self.setup_status.set("自动配置未完成：" + event["error"])
                self.append_log("自动配置未完成：" + event["error"])
            elif kind in ("message", "notice", "event"):
                self.model.receive(event)
            elif kind == "send_result":
                self.model.complete(event)
            elif kind in ("send_started", "api_frame"):
                if kind == "api_frame":
                    self.console_api_event(event)
                self.emit(event)
            elif kind == "status":
                self.status = event["status"]
                self.emit(event)
                self.append_log("连接状态：" + self.status)
                if self.status == "connected":
                    self.network.catalog()
            elif kind == "catalog":
                for item in event["items"]:
                    try:
                        group = event["kind"] == "group"
                        target = item["group_id" if group else "user_id"]
                        name = item.get("group_name", "") if group else (item.get("remark") or item.get("nickname", ""))
                        self.model.discover(f"qq:{event['kind']}:{target}", name)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                self.persist()
            elif kind == "log":
                self.append_log(event["text"])
            dirty = True
        if dirty:
            self.refresh()
        if not self.closing:
            self.timer = self.root.after(80, self.pump)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.root.after_cancel(self.timer)
        if self.setup_timer is not None:
            self.root.after_cancel(self.setup_timer)
        self.model.shutdown()
        self.network.stop()
        logging.getLogger().removeHandler(self.log_handler)
        self.root.withdraw()
        deadline = time.monotonic() + 2.5
        def finish():
            if self.network.thread and self.network.thread.is_alive() and time.monotonic() < deadline:
                self.root.after(50, finish)
            else:
                self.root.destroy()
        finish()


def run(args, emit):
    def safe_emit(event):
        # 父进程异常退出后 stdout 可能先于 stdin EOF 关闭，清理不能被回执写入打断。
        try:
            emit(event)
        except (BrokenPipeError, OSError, ValueError):
            pass

    defaults = dict(url=args.url, api_timeout=args.api_timeout,
                    reconnect_delay=args.reconnect_delay, require_at=args.require_at)
    path = os.getenv("NAPCAT_PANEL_CONFIG") or str(Path(__file__).parent / ".config" / "panel.json")
    try:
        settings = Settings(path, defaults)
        # 旧环境允许列表只用于发现会话，首次使用仍须手动启用。
        for kind, values in (("group", args.allow_group), ("private", args.allow_user)):
            for target in values or ():
                settings.sessions.setdefault(f"qq:{kind}:{target}", dict(name="", enabled=False))
        root = tk.Tk()
        token = os.getenv("NAPCAT_TOKEN", "") or saved_token(settings.napcat_config_path, settings.connection["url"])
        panel = Panel(root, settings, safe_emit, token=token)
    except (ValueError, OSError, tk.TclError) as exc:
        logging.error("界面启动失败：%s", type(exc).__name__)
        return 1

    def read():
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except ValueError:
                    continue
                if isinstance(command, dict) and command.get("type") == "shutdown":
                    break
                panel.events.put(dict(type="command", command=command))
        except (OSError, UnicodeError):
            pass
        finally:
            panel.events.put(dict(type="shutdown"))
    threading.Thread(target=read, name="napcat-stdin", daemon=True).start()
    safe_emit(dict(type="ready", protocol_version=1))
    try:
        root.mainloop()
    except KeyboardInterrupt:
        panel.close()
        root.mainloop()
    return 0
