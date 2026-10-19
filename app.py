"""FlowGet desktop, independently implemented from a black-box specification."""
from __future__ import annotations

import ctypes
from datetime import datetime
import os
import subprocess
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from engine import Manager, ACTIVE

VERSION = "0.2.0"
BG, PANEL, INK, MUTED, ACCENT = "#11151d", "#1b2230", "#e5ebf6", "#93a2b9", "#58d5bc"
STATUS = {"queued": "等待队列", "probing": "连接中", "downloading": "下载中", "pausing": "正在暂停",
          "paused": "已暂停", "verifying": "合并 / 校验", "complete": "已完成", "error": "失败"}


def size(n):
    if n is None:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024


def eta(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def open_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    target = str(Path(path).resolve())
    if sys.platform == "win32":
        os.startfile(target)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])


class Dialog(tk.Toplevel):
    def __init__(self, app, title, width=620):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app.root)
        self.body = ttk.Frame(self, padding=25, style="Panel.TFrame")
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.geometry(f"{width}x400+{app.root.winfo_x()+150}+{app.root.winfo_y()+90}")
        self.grab_set()
        self.after_idle(self.focus_set)
        self.bind("<Escape>", lambda e: self.destroy())

    def label(self, text, row):
        ttk.Label(self.body, text=text, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 5))

    def destroy(self):
        if getattr(self, "update_timer", None):
            self.after_cancel(self.update_timer)
        super().destroy()


class AddDialog(Dialog):
    def __init__(self, app, initial=""):
        super().__init__(app, "新建下载 · FlowGet", 650)
        self.geometry(f"650x630+{app.root.winfo_x()+110}+{app.root.winfo_y()+40}")
        self.label("下载地址 · HTTP / HTTPS", 0)
        self.url = tk.StringVar(value=initial)
        url_entry = ttk.Entry(self.body, textvariable=self.url, width=72)
        url_entry.grid(row=1, column=0, sticky="ew")
        self.after_idle(url_entry.focus_set)
        self.label("文件名 · 留空则采用网址中的名称", 2)
        self.name = tk.StringVar()
        ttk.Entry(self.body, textvariable=self.name).grid(row=3, column=0, sticky="ew")
        self.label("保存目录", 4)
        folder_row = ttk.Frame(self.body, style="Panel.TFrame")
        folder_row.grid(row=5, column=0, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        self.folder = tk.StringVar(value=app.manager.settings["folder"])
        ttk.Entry(folder_row, textvariable=self.folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="浏览…", command=self.browse).grid(row=0, column=1, padx=(8, 0))
        self.label("最大连接数（1–32）", 6)
        self.connections = tk.IntVar(value=app.manager.settings["connections"])
        ttk.Spinbox(self.body, from_=1, to=32, textvariable=self.connections, width=8).grid(row=7, column=0, sticky="w")
        self.label("预期 SHA-256 · 可选，填写后必须匹配才保存成品", 8)
        self.digest = tk.StringVar()
        ttk.Entry(self.body, textvariable=self.digest).grid(row=9, column=0, sticky="ew")
        self.label("单任务速度上限 · KB/s，0 表示不限速", 10)
        self.limit = tk.IntVar(value=0)
        ttk.Spinbox(self.body, from_=0, to=1048576, textvariable=self.limit, width=16).grid(row=11, column=0, sticky="w")
        footer = ttk.Frame(self.body, style="Panel.TFrame")
        footer.grid(row=12, column=0, sticky="e", pady=(24, 0))
        ttk.Button(footer, text="稍后下载", command=lambda: self.submit(False)).pack(side="left", padx=8)
        ttk.Button(footer, text="加入下载队列", style="Accent.TButton", command=lambda: self.submit(True)).pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())

    def browse(self):
        value = filedialog.askdirectory(parent=self, initialdir=self.folder.get())
        if value:
            self.folder.set(value)

    def submit(self, start):
        try:
            if not self.folder.get().strip():
                raise ValueError("请选择保存目录")
            c = self.connections.get()
            if not 1 <= c <= 32:
                raise ValueError("连接数需介于 1 和 32 之间")
            task_id = self.app.manager.add(self.url.get(), self.folder.get(), self.name.get(), c,
                self.digest.get(), start=start, limit_kib=self.limit.get())
            self.app.query.set("")
            self.app.select_filter("全部任务")
            self.app.table.selection_set(task_id)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)


class EditDialog(Dialog):
    def __init__(self, app, task):
        super().__init__(app, "编辑任务 / 更新下载地址", 680)
        self.task_id = task["id"]
        self.geometry(f"680x560+{app.root.winfo_x()+110}+{app.root.winfo_y()+40}")
        self.variables = {}
        for row, (key, title) in enumerate((("url", "下载地址"), ("name", "文件名"),
                ("connections", "最大连接数 · 1–32"), ("limit_kib", "单任务限速 · KB/s，0 表示不限速"))):
            self.label(title, row * 2)
            self.variables[key] = tk.StringVar(value=str(task.get(key, 0)))
            ttk.Entry(self.body, textvariable=self.variables[key]).grid(row=row*2+1, column=0, sticky="ew")
        ttk.Label(self.body, text="更新地址后会重新下载，避免混合不同文件的分段。\n仅调整文件名或限速时，仍会校验并尝试续传。\n连接数调整在下一次建立全新分段时生效。",
            style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=18)
        ttk.Button(self.body, text="保存为暂停任务", style="Accent.TButton", command=self.submit).grid(row=9, column=0, sticky="e")

    def submit(self):
        try:
            self.app.manager.update_task(self.task_id, **{k: v.get() for k, v in self.variables.items()})
            self.app.refresh_selection()
            self.destroy()
        except Exception as exc:
            messagebox.showerror("无法保存", str(exc), parent=self)


class Settings(Dialog):
    def __init__(self, app):
        super().__init__(app, "下载设置 · FlowGet")
        self.geometry(f"650x640+{app.root.winfo_x()+110}+{app.root.winfo_y()+30}")
        self.variables = {}
        for row, (key, title, lower, upper) in enumerate([
            ("connections", "新任务默认连接数", 1, 32),
            ("parallel", "同时下载的任务数", 1, 8),
            ("limit_kib", "总速度上限 · KB/s，0 表示不限速", 0, 1048576)]):
            self.label(title, row * 2)
            self.variables[key] = tk.IntVar(value=app.manager.settings[key])
            ttk.Spinbox(self.body, from_=lower, to=upper, textvariable=self.variables[key], width=16).grid(row=row * 2 + 1, column=0, sticky="w")
        self.label("默认保存目录", 6)
        self.folder = tk.StringVar(value=app.manager.settings["folder"])
        ttk.Entry(self.body, textvariable=self.folder).grid(row=7, column=0, sticky="ew")
        self.label("队列定时启动 · 本机时间，格式：2026-10-19 23:30", 8)
        value = app.manager.settings.get("schedule_at", "")
        self.schedule = tk.StringVar(value=datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M") if value else "")
        ttk.Entry(self.body, textvariable=self.schedule).grid(row=9, column=0, sticky="ew")
        self.label("队列定时停止 · 暂停传输，留空取消", 10)
        stop = app.manager.settings.get("stop_at", "")
        self.stop_at = tk.StringVar(value=datetime.fromtimestamp(float(stop)).strftime("%Y-%m-%d %H:%M") if stop else "")
        ttk.Entry(self.body, textvariable=self.stop_at).grid(row=11, column=0, sticky="ew")
        ttk.Label(self.body, text="定时功能需要程序保持运行；停止后，活跃任务需手动继续。", style="Muted.TLabel").grid(row=12, column=0, sticky="w", pady=(15, 0))
        ttk.Button(self.body, text="保存设置", style="Accent.TButton", command=self.submit).grid(row=13, column=0, sticky="e", pady=(20, 0))

    def submit(self):
        try:
            values = {key: v.get() for key, v in self.variables.items()}
            if not 1 <= values["connections"] <= 32 or not 1 <= values["parallel"] <= 8 or values["limit_kib"] < 0:
                raise ValueError("请检查连接数、同时下载数和限速范围")
            values["folder"] = str(Path(self.folder.get()).resolve())
            text = self.schedule.get().strip()
            values["schedule_at"] = datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() if text else ""
            if text:
                if values["schedule_at"] <= datetime.now().timestamp():
                    raise ValueError("请选择未来的启动时间")
                values["queue_enabled"] = False
            stop_text = self.stop_at.get().strip()
            values["stop_at"] = datetime.strptime(stop_text, "%Y-%m-%d %H:%M").timestamp() if stop_text else ""
            if stop_text and values["stop_at"] <= datetime.now().timestamp():
                raise ValueError("请选择未来的停止时间")
            if text and stop_text and values["stop_at"] <= values["schedule_at"]:
                raise ValueError("停止时间应晚于启动时间")
            if not self.folder.get().strip():
                raise ValueError("请选择默认保存目录")
            self.app.manager.configure(**values)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("设置未保存", str(exc), parent=self)


class App:
    def __init__(self, root, manager):
        self.root, self.manager = root, manager
        self.filter = "全部任务"
        self.history = [0] * 80
        self.rows = {}
        self.sort_key, self.sort_reverse = None, False
        self.closing = False
        root.title(f"FlowGet · 下载管理器 {VERSION}")
        root.geometry("1280x820")
        root.minsize(1060, 740)
        root.configure(bg=BG)
        self.style()
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-n>", lambda e: self.new())
        root.bind("<Control-f>", lambda e: self.search.focus_set())
        root.bind("<Control-v>", self.paste)
        self.refresh()

    def style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background=BG, foreground=INK,
                        borderwidth=0, focuscolor=ACCENT)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL)
        style.configure("Muted.TLabel", foreground=MUTED, background=PANEL, font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", fieldbackground="#252e3e", foreground=INK, insertcolor=INK, padding=9)
        style.configure("TEntry", bordercolor="#354055", lightcolor="#354055", darkcolor="#354055")
        style.configure("TSpinbox", fieldbackground="#252e3e", foreground=INK, padding=7, arrowsize=16)
        style.configure("TButton", background="#293448", foreground=INK, padding=(15, 9))
        style.map("TButton", background=[("active", "#35455e")])
        style.map("TButton", foreground=[("disabled", "#617086")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#082c25", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#82edd8")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK,
                        rowheight=44, borderwidth=0, lightcolor=PANEL, darkcolor=PANEL, bordercolor=PANEL, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#222b3b", foreground=MUTED, padding=(8, 12), font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#294b54")], foreground=[("selected", "#ecfff9")])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#2a3445", borderwidth=0,
            lightcolor=ACCENT, darkcolor=ACCENT, bordercolor="#2a3445", thickness=5)
        style.configure("Vertical.TScrollbar", background="#354055", troughcolor=PANEL, arrowsize=12)
        style.configure("Compact.TButton", padding=(10, 5), font=("Microsoft YaHei UI", 9))

    def build(self):
        menubar = tk.Menu(self.root)
        tasks_menu = tk.Menu(menubar, tearoff=False)
        tasks_menu.add_command(label="新建下载…", command=self.new)
        tasks_menu.add_command(label="批量导入链接…", command=self.import_links)
        tasks_menu.add_command(label="导出所有链接…", command=self.export_links)
        tasks_menu.add_separator()
        tasks_menu.add_command(label="开始 / 停止队列", command=self.queue)
        tasks_menu.add_command(label="下载设置…", command=lambda: Settings(self))
        menubar.add_cascade(label="任务", menu=tasks_menu)
        self.root.configure(menu=menubar)
        side = tk.Frame(self.root, bg="#151b25", width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="FlowGet", font=("Segoe UI", 24, "bold"), bg="#151b25", fg=ACCENT).pack(anchor="w", padx=24, pady=(25, 0))
        tk.Label(side, text="下载，由你掌控", font=("Microsoft YaHei UI", 9), bg="#151b25", fg=MUTED).pack(anchor="w", padx=25, pady=(2, 34))
        self.nav = {}
        for title in ("全部任务", "正在下载", "等待队列", "已暂停", "已完成", "失败", "程序", "视频", "音乐", "文档", "压缩文件"):
            if title == "程序":
                tk.Label(side, text="文件分类", bg="#151b25", fg="#617086", font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x", padx=26, pady=(18, 6))
            button = tk.Button(side, text=title, anchor="w", relief="flat", bd=0,
                highlightthickness=0,
                font=("Microsoft YaHei UI", 10), padx=16, pady=8,
                bg="#151b25", fg=MUTED, activebackground="#253344", activeforeground=INK,
                command=lambda x=title: self.select_filter(x))
            button.pack(fill="x", padx=10, pady=1)
            self.nav[title] = button
        tk.Button(side, text="关于 / 实现进度", command=self.about, bg="#151b25", fg=MUTED,
                  relief="flat", highlightthickness=0, font=("Microsoft YaHei UI", 9), pady=16).pack(side="bottom", fill="x")
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=26, pady=23)
        heading = tk.Frame(main, bg=BG)
        heading.pack(fill="x")
        self.heading = tk.Label(heading, text="全部任务", bg=BG, fg=INK, font=("Microsoft YaHei UI", 22, "bold"))
        self.heading.pack(side="left")
        self.query = tk.StringVar()
        self.search = ttk.Entry(heading, textvariable=self.query, width=25)
        self.search.pack(side="right", pady=5)
        tk.Label(heading, text="搜索文件或地址  ", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right")
        self.search.insert(0, "")
        self.query.trace_add("write", lambda *_: self.render_table())
        tk.Label(main, text="多连接传输   /   安全续传   /   本地任务记录", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 20))
        cards = tk.Frame(main, bg=BG)
        cards.pack(fill="x", pady=(0, 18))
        self.metrics = []
        for i, title in enumerate(("当前总速度", "正在传输", "已完成任务")):
            box = tk.Frame(cards, bg=PANEL, padx=18, pady=12)
            box.pack(side="left", fill="x", expand=True, padx=(0, 12 if i < 2 else 0))
            tk.Label(box, text=title, bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            label = tk.Label(box, text="0", bg=PANEL, fg=ACCENT if i == 0 else INK, font=("Segoe UI", 20, "bold"))
            label.pack(anchor="w", pady=(5, 0))
            self.metrics.append(label)
        tools = tk.Frame(main, bg=BG)
        tools.pack(fill="x", pady=(0, 12))
        self.actions = {}
        for title, command, style in [
            ("＋ 新建下载", self.new, "Accent.TButton"), ("继续", self.resume, "TButton"),
            ("暂停", self.pause, "TButton"), ("队列", self.queue, "TButton"),
            ("保存目录", self.folder, "TButton"), ("设置", lambda: Settings(self), "TButton")]:
            button = ttk.Button(tools, text=title, command=command, style=style)
            button.pack(side="left", padx=(0, 7))
            self.actions[title] = button
        self.queue_label = tk.Label(tools, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.queue_label.pack(side="right")
        selection_bar = tk.Frame(main, bg=BG)
        selection_bar.pack(fill="x", pady=(0, 10))
        self.selection_label = tk.Label(selection_bar, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.selection_label.pack(side="left")
        action_group = tk.Frame(selection_bar, bg=BG)
        action_group.pack(side="right")
        for title, command in (("↑ 上移", lambda: self.move(-1)), ("↓ 下移", lambda: self.move(1)),
                ("编辑 / 更新地址", self.edit), ("移除", self.remove)):
            button = ttk.Button(action_group, text=title, command=command, style="Compact.TButton")
            button.pack(side="left", padx=(5, 0))
            self.actions[title] = button
        table_box = tk.Frame(main, bg=PANEL)
        table_box.pack(fill="both", expand=True)
        columns = ("order", "name", "size", "progress", "state", "speed", "eta")
        self.table = ttk.Treeview(table_box, columns=columns, show="headings", selectmode="extended", height=6)
        self.column_titles = dict(zip(columns, ("顺序", "文件名", "大小", "进度", "状态", "速度", "剩余时间")))
        for c, width, stretch in zip(columns, (55, 270, 90, 85, 110, 95, 95), (False, True, False, False, False, False, False)):
            self.table.heading(c, text=self.column_titles[c], command=lambda key=c: self.sort_by(key))
            self.table.column(c, width=width, minwidth=55 if c == "order" else 75, stretch=stretch, anchor="w" if c == "name" else "center")
        table_box.rowconfigure(0, weight=1)
        table_box.columnconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_box, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(table_box, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(xscrollcommand=horizontal.set)
        self.table.tag_configure("complete", foreground=ACCENT)
        self.table.tag_configure("error", foreground="#ff969d")
        self.table.tag_configure("downloading", foreground="#8ce5d2")
        self.table.tag_configure("stripe", background="#1e2736")
        self.table.bind("<<TreeviewSelect>>", lambda e: self.details())
        self.table.bind("<Double-1>", lambda e: self.details_popup())
        self.table.bind("<Button-3>", self.context)
        self.table.bind("<Delete>", lambda e: self.remove())
        self.table.bind("<Control-a>", lambda e: self.table.selection_set(self.table.get_children()))
        self.table.bind("<Return>", lambda e: self.details_popup())
        self.empty = tk.Label(table_box, text="准备好下一次下载\n\n点击「新建下载」，或按 Ctrl+V 粘贴链接", bg=PANEL, fg=MUTED,
                              font=("Microsoft YaHei UI", 12), justify="center")
        lower = tk.Frame(main, bg=PANEL, padx=16, pady=12)
        lower.pack(fill="x", pady=(12, 0))
        graph_box = tk.Frame(lower, bg=PANEL, width=240)
        graph_box.pack(side="right", fill="y", padx=(18, 0))
        tk.Label(graph_box, text="总下载速度 · 最近 40 秒", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self.graph = tk.Canvas(graph_box, width=240, height=98, bg=PANEL, highlightthickness=0)
        self.graph.pack()
        info = tk.Frame(lower, bg=PANEL)
        info.pack(side="left", fill="both", expand=True)
        self.detail_name = tk.Label(info, text="任务详情", bg=PANEL, fg=INK, font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.detail_name.pack(fill="x")
        self.progress = ttk.Progressbar(info, maximum=100)
        self.progress.pack(fill="x", pady=(10, 8))
        self.detail_text = tk.Label(info, text="选择任务，查看连接与校验状态", bg=PANEL, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left", wraplength=520)
        self.detail_text.pack(fill="x")
        self.segment_canvas = tk.Canvas(info, height=16, bg=PANEL, highlightthickness=0)
        self.segment_canvas.pack(fill="x", pady=(8, 0))
        self.footer = tk.Label(main, text="", bg=BG, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
        self.footer.pack(fill="x", pady=(10, 0))

    def select_filter(self, value):
        self.filter = value
        self.heading.configure(text=value)
        self.render_table()

    def accept(self, t, group=None, search=True):
        group = group or self.filter
        if search and self.query.get().lower() not in (t["name"] + " " + t["url"]).lower():
            return False
        if group == "全部任务":
            return True
        if group == "正在下载":
            return t["status"] in ACTIVE
        if group == "等待队列":
            return t["status"] == "queued"
        if group in ("已暂停", "已完成", "失败"):
            return t["status"] == {"已暂停": "paused", "已完成": "complete", "失败": "error"}[group]
        ext = Path(t["name"]).suffix.lower()
        return ext in {"程序": {".exe", ".msi", ".apk", ".msix"}, "视频": {".mp4", ".mkv", ".webm", ".avi", ".mov"},
            "音乐": {".mp3", ".flac", ".wav", ".m4a", ".ogg"}, "文档": {".pdf", ".docx", ".txt", ".xlsx", ".epub"},
            "压缩文件": {".zip", ".7z", ".rar", ".gz", ".tar"}}.get(group, set())

    def render_table(self):
        self.rows = {t["id"]: t for t in self.manager.snapshots()}
        positions = {key: i+1 for i, key in enumerate(self.rows)}
        wanted = [t for t in self.rows.values() if self.accept(t)]
        if self.sort_key:
            key = self.sort_key
            def sort_value(t):
                return {"name": t["name"].casefold(), "size": t["total"] if t["total"] is not None else -1,
                    "progress": t["done"] / t["total"] if t["total"] else int(t["status"] == "complete"),
                    "state": STATUS.get(t["status"], t["status"]), "speed": t["speed"],
                    "eta": max(0, t["total"] - t["done"]) / t["speed"] if t["total"] is not None and t["speed"] else float("inf")}[key]
            wanted.sort(key=sort_value, reverse=self.sort_reverse)
        ids = {t["id"] for t in wanted}
        for old in self.table.get_children():
            if old not in ids:
                self.table.delete(old)
        for index, t in enumerate(wanted):
            fraction = t["done"] / t["total"] if t["total"] else (1 if t["status"] == "complete" else 0)
            speed = t["speed"]
            remaining = max(0, t["total"] - t["done"]) / speed if t["total"] is not None and speed > 0 else None
            values = (positions[t["id"]], t["name"], size(t["total"]), f"{fraction:.1%}" if t["total"] is not None else "—", STATUS.get(t["status"], t["status"]),
                      size(speed) + "/s" if speed else "—", eta(remaining))
            tags = (t["status"], "stripe" if index % 2 else "plain")
            if self.table.exists(t["id"]):
                self.table.item(t["id"], values=values, tags=tags)
            else:
                self.table.insert("", "end", iid=t["id"], values=values, tags=tags)
            self.table.move(t["id"], "", index)
        if wanted:
            self.empty.place_forget()
        else:
            self.empty.configure(text="没有匹配的任务\n\n调整搜索或切换左侧分类" if self.rows else "准备好下一次下载\n\n点击「新建下载」，或按 Ctrl+V 粘贴链接")
            self.empty.place(relx=0.5, rely=0.48, anchor="center")
        for title, button in self.nav.items():
            count = sum(self.accept(t, title, search=False) for t in self.rows.values())
            button.configure(text=f"{title}   {count}", bg="#263847" if title == self.filter else "#151b25", fg=ACCENT if title == self.filter else MUTED)
        self.selection_label.configure(text=f"显示 {len(wanted)} / {len(self.rows)} 项 · 已选 {len(self.selected())} 项")

    def sort_by(self, key):
        self.sort_reverse = not self.sort_reverse if self.sort_key == key else False
        self.sort_key = None if key == "order" else key
        for column, title in self.column_titles.items():
            self.table.heading(column, text=title + (" ↓" if self.sort_reverse else " ↑") if column == self.sort_key else title)
        self.render_table()

    def refresh_selection(self):
        self.render_table()
        self.details()

    def move(self, direction):
        self.manager.move([t["id"] for t in self.selected()], direction)
        self.sort_by("order")
        self.details()

    def edit(self):
        tasks = self.selected()
        if len(tasks) == 1:
            if tasks[0]["status"] in ACTIVE or tasks[0]["status"] == "complete":
                messagebox.showinfo("编辑任务", "请先暂停未完成任务，等待连接停止。", parent=self.root)
                return
            EditDialog(self, tasks[0])

    def selected(self):
        return [self.rows[i] for i in self.table.selection() if i in self.rows]

    def details(self):
        tasks = self.selected()
        states = {t["status"] for t in tasks}
        enabled = {"继续": bool(states & {"paused", "error"}), "暂停": bool(states & (ACTIVE | {"queued"})),
            "编辑 / 更新地址": len(tasks) == 1 and not states & (ACTIVE | {"complete"}),
            "移除": bool(tasks) and not states & ACTIVE, "↑ 上移": bool(tasks), "↓ 下移": bool(tasks)}
        for title, active in enabled.items():
            self.actions[title].state(["!disabled" if active else "disabled"])
        self.selection_label.configure(text=f"显示 {len(self.table.get_children())} / {len(self.rows)} 项 · 已选 {len(tasks)} 项")
        self.segment_canvas.delete("all")
        if not tasks:
            self.detail_name.configure(text="任务详情")
            self.detail_text.configure(text="选择任务，查看连接与校验状态")
            self.progress["value"] = 0
            return
        t = tasks[0]
        self.detail_name.configure(text=(f"已选 {len(tasks)} 项 · " if len(tasks) > 1 else "") + t["name"])
        fraction = t["done"] / t["total"] if t["total"] else (1 if t["status"] == "complete" else 0)
        self.progress["value"] = fraction * 100
        text = t["error"] or ("SHA-256  " + t["sha256"] if t["sha256"] else
            f"{size(t['done'])} / {size(t['total'])}   ·   {len(t['segments'])} 个分段   ·   " +
            ("支持校验续传" if t["resumable"] else "缺少强文件标识时使用单连接，重新开始可安全重下"))
        limit = f"单任务上限 {t.get('limit_kib', 0):,} KB/s" if t.get("limit_kib") else "单任务不限速"
        self.detail_text.configure(text=text + "\n" + limit, fg="#ff969d" if t["error"] else MUTED)
        count = len(t["segments"])
        width = max(200, self.segment_canvas.winfo_width())
        for i, segment in enumerate(t["segments"]):
            x0, x1 = i * width / count, (i + 1) * width / count - 3
            self.segment_canvas.create_rectangle(x0, 1, x1, 12, fill="#2c394d", outline="")
            length = segment["end"] - segment["start"] + 1 if segment["end"] is not None else 0
            part = min(1, segment["done"] / length) if length else 0
            self.segment_canvas.create_rectangle(x0, 1, x0 + (x1-x0)*part, 12, fill=ACCENT, outline="")

    def refresh(self):
        self.render_table()
        tasks = list(self.rows.values())
        speed = sum(t["speed"] for t in tasks)
        self.metrics[0].configure(text=size(speed) + "/s")
        self.history = self.history[1:] + [speed]
        self.draw_graph()
        self.metrics[1].configure(text=f"{sum(t['status'] in ACTIVE for t in tasks):02}")
        self.metrics[2].configure(text=f"{sum(t['status'] == 'complete' for t in tasks):02}")
        scheduled = self.manager.settings.get("schedule_at")
        queue_text = "定时 " + datetime.fromtimestamp(float(scheduled)).strftime("%m-%d %H:%M") if scheduled else ("队列开启" if self.manager.settings["queue_enabled"] else "队列停止")
        self.queue_label.configure(text=queue_text, fg=ACCENT if self.manager.settings["queue_enabled"] else MUTED)
        self.actions["队列"].configure(text="停止队列" if self.manager.settings["queue_enabled"] else "启动队列")
        stop_at = self.manager.settings.get("stop_at")
        suffix = "    ·    " + datetime.fromtimestamp(float(stop_at)).strftime("%m-%d %H:%M 定时停止") if stop_at else ""
        self.footer.configure(text="任务保存失败：" + self.manager.last_save_error if self.manager.last_save_error else
            f"FlowGet {VERSION}    ·    Ctrl+N 新建 / Ctrl+F 搜索 / Delete 移除" + suffix)
        self.details()
        if not self.closing:
            self.refresh_timer = self.root.after(500, self.refresh)

    def draw_graph(self):
        self.graph.delete("all")
        width, height = 240, 98
        peak = max(max(self.history), 1024)
        for y in (24, 49, 74):
            self.graph.create_line(0, y, width, y, fill="#2a3445", dash=(2, 4))
        points = [(i * width / (len(self.history)-1), height-8 - value / peak * (height-30)) for i, value in enumerate(self.history)]
        self.graph.create_polygon(0, height, *[v for p in points for v in p], width, height, fill="#203e40", outline="")
        self.graph.create_line(*[v for p in points for v in p], fill=ACCENT, width=2)
        self.graph.create_text(4, 6, anchor="nw", text=size(max(self.history)) + "/s 峰值", fill=MUTED, font=("Segoe UI", 8))

    def new(self):
        AddDialog(self)

    def import_links(self):
        path = filedialog.askopenfilename(parent=self.root, title="导入链接列表（每行一个 HTTP/HTTPS 地址）",
            filetypes=[("链接列表", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            if Path(path).stat().st_size > 2 * 1024 * 1024:
                raise ValueError("本版链接列表限制为 2 MB")
            urls = list(dict.fromkeys(line.strip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")))
            if len(urls) > 1000:
                raise ValueError("一次最多导入 1000 个链接")
            accepted, failed = 0, 0
            for url in urls:
                try:
                    # Import paused so the user can review before transferring.
                    self.manager.add(url, start=False)
                    accepted += 1
                except ValueError:
                    failed += 1
            messagebox.showinfo("导入完成", f"已添加 {accepted} 个暂停任务，跳过 {failed} 个无效地址。\n选择任务后点击继续下载。", parent=self.root)
        except Exception as exc:
            messagebox.showerror("无法导入", str(exc), parent=self.root)

    def export_links(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="导出所有下载链接", defaultextension=".txt",
            initialfile="FlowGet-links.txt", filetypes=[("链接列表", "*.txt")])
        if not path:
            return
        try:
            Path(path).write_text("\n".join(t["url"] for t in self.manager.snapshots()) + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("无法导出", str(exc), parent=self.root)

    def paste(self, event=None):
        if self.root.focus_get() in (self.search,) or isinstance(self.root.focus_get(), (ttk.Entry, tk.Entry, tk.Text)):
            return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if text.startswith(("http://", "https://")):
            AddDialog(self, text)
            return "break"

    def resume(self):
        for t in self.selected():
            self.manager.resume(t["id"])
        self.refresh_selection()

    def pause(self):
        for t in self.selected():
            self.manager.pause(t["id"])
        self.refresh_selection()

    def queue(self):
        enabled = not self.manager.settings["queue_enabled"]
        self.manager.set_queue(enabled)
        self.refresh_selection()

    def folder(self):
        tasks = self.selected()
        open_directory(tasks[0]["folder"] if tasks else self.manager.settings["folder"])

    def details_popup(self):
        tasks = self.selected()
        if not tasks:
            return
        t = tasks[0]
        dialog = Dialog(self, "任务属性 · 实时连接", 760)
        dialog.geometry(f"760x520+{self.root.winfo_x()+80}+{self.root.winfo_y()+60}")
        notebook = ttk.Notebook(dialog.body)
        notebook.pack(fill="both", expand=True)
        summary, connections = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(summary, text="  文件与校验  ")
        notebook.add(connections, text="  分段连接  ")
        text = tk.Text(summary, bg="#252e3e", fg=INK, wrap="word", font=("Microsoft YaHei UI", 10), relief="flat", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        parts = ttk.Treeview(connections, columns=("index", "range", "done", "progress"), show="headings")
        for key, title, width in (("index", "分段", 60), ("range", "字节范围", 250), ("done", "已传输", 130), ("progress", "进度", 100)):
            parts.heading(key, text=title)
            parts.column(key, width=width, anchor="center")
        scroll = ttk.Scrollbar(connections, orient="vertical", command=parts.yview)
        scroll.pack(side="right", fill="y")
        parts.configure(yscrollcommand=scroll.set)
        parts.pack(fill="both", expand=True)
        def update():
            if not dialog.winfo_exists():
                return
            current = next((row for row in self.manager.snapshots() if row["id"] == t["id"]), None)
            if current is None:
                dialog.destroy()
                return
            text.configure(state="normal")
            # Preserve text selection while the user copies a URL or hash.
            content = (f"文件：{current['name']}\n状态：{STATUS[current['status']]}\n地址：{current['url']}\n"
                f"目录：{current['folder']}\n已下载：{size(current['done'])} / {size(current['total'])}\n"
                f"速度：{size(current['speed'])}/s\n续传：{'支持' if current['resumable'] else '尚未确认或不支持'}\n"
                f"SHA-256：{current['sha256'] or '完成后计算'}\n预期 SHA-256：{current['expected_sha256'] or '未指定'}\n\n{current['error']}")
            if text.get("1.0", "end-1c") != content:
                text.delete("1.0", "end")
                text.insert("1.0", content)
            text.configure(state="disabled")
            ids = set()
            for i, segment in enumerate(current["segments"]):
                key = str(i)
                ids.add(key)
                length = segment["end"] - segment["start"] + 1 if segment["end"] is not None else None
                values = (i+1, f"{segment['start']:,} – {segment['end'] if segment['end'] is not None else '?'}", size(segment["done"]),
                    f"{segment['done'] / length:.1%}" if length else "未知大小")
                if parts.exists(key):
                    parts.item(key, values=values)
                else:
                    parts.insert("", "end", iid=key, values=values)
            for old in parts.get_children():
                if old not in ids:
                    parts.delete(old)
            dialog.update_timer = dialog.after(500, update)
        update()

    def context(self, event):
        item = self.table.identify_row(event.y)
        if item:
            if item not in self.table.selection():
                self.table.selection_set(item)
        else:
            return
        menu = tk.Menu(self.root, tearoff=False)
        for title, command in [("继续 / 重试", self.resume), ("暂停", self.pause), ("打开保存目录", self.folder),
                ("任务属性", self.details_popup), ("编辑 / 更新下载地址", self.edit),
                ("上移队列", lambda: self.move(-1)), ("下移队列", lambda: self.move(1)),
                ("复制下载地址", self.copy_url), ("移除任务（保留文件）", self.remove)]:
            menu.add_command(label=title, command=command)
        menu.tk_popup(event.x_root, event.y_root)

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(t["url"] for t in self.selected()))

    def remove(self):
        if not self.selected():
            return
        if not messagebox.askyesno("移除任务", "从列表移除选中任务？已下载文件和临时分段都会保留。", parent=self.root):
            return
        try:
            for t in self.selected():
                self.manager.remove(t["id"])
            self.refresh_selection()
        except ValueError as exc:
            messagebox.showinfo("请稍候", str(exc), parent=self.root)

    def about(self):
        messagebox.showinfo("FlowGet", f"FlowGet {VERSION}\n\n独立实现的 Windows 下载管理器。\n已实现：分段、续传、队列排序、定时启停、全局及单任务限速、\n链接更新、校验、批量链接、速度曲线和实时连接详情。\n已提供：浏览器主动发送链接扩展，需单独安装和联调。\n\n后续：自动接管、请求上下文、站点抓取、流媒体。\n本程序与 Tonec / IDM 无隶属关系。", parent=self.root)

    def close(self):
        self.closing = True
        self.root.after_cancel(self.refresh_timer)
        self.footer.configure(text="正在保存任务、停止连接，请稍候…")
        self.root.withdraw()
        def stop():
            try:
                self.manager.shutdown()
            finally:
                self.root.after(0, self.root.destroy)
        threading.Thread(target=stop, daemon=True).start()


def main():
    # Let Windows scale this Tk 8.6 layout as a whole; declaring system DPI
    # awareness scales point fonts but not Tk pixel geometry at 200% displays.
    data_dir = Path(os.environ.get("FLOWGET_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FlowGet")
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (data_dir / "instance.lock").open("a+b")
    if sys.platform == "win32":
        import msvcrt
        try:
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if "--from-browser" not in sys.argv:
                ctypes.windll.user32.MessageBoxW(0, "FlowGet 已在运行，请切换到已有窗口。", "FlowGet", 0)
            return
    root = tk.Tk()
    try:
        manager = Manager(data_dir)
        App(root, manager)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("FlowGet 无法启动", str(exc), parent=root)
        raise
    finally:
        lock_file.close()


if __name__ == "__main__":
    main()
