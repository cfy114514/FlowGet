"""FlowGet desktop, independently implemented from a black-box specification."""
from __future__ import annotations

import ctypes
from datetime import datetime
import os
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from engine import Manager, ACTIVE

VERSION = "0.1.0"
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
    os.startfile(str(Path(path).resolve()))


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

    def label(self, text, row):
        ttk.Label(self.body, text=text, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 5))


class AddDialog(Dialog):
    def __init__(self, app, initial=""):
        super().__init__(app, "新建下载 · FlowGet", 650)
        self.geometry(f"650x540+{app.root.winfo_x()+110}+{app.root.winfo_y()+60}")
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
        footer = ttk.Frame(self.body, style="Panel.TFrame")
        footer.grid(row=10, column=0, sticky="e", pady=(24, 0))
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
                self.digest.get(), start=start)
            self.app.filter = "全部任务"
            self.app.render_table()
            self.app.table.selection_set(task_id)
            self.destroy()
        except Exception as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)


class Settings(Dialog):
    def __init__(self, app):
        super().__init__(app, "下载设置 · FlowGet")
        self.geometry(f"620x500+{app.root.winfo_x()+150}+{app.root.winfo_y()+60}")
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
        self.label("队列定时启动 · 留空不设置，格式：2026-10-19 23:30", 8)
        value = app.manager.settings.get("schedule_at", "")
        self.schedule = tk.StringVar(value=datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M") if value else "")
        ttk.Entry(self.body, textvariable=self.schedule).grid(row=9, column=0, sticky="ew")
        ttk.Button(self.body, text="保存设置", style="Accent.TButton", command=self.submit).grid(row=10, column=0, sticky="e", pady=(20, 0))

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
        self.closing = False
        root.title(f"FlowGet · 下载管理器 {VERSION}")
        root.geometry("1200x760")
        root.minsize(980, 640)
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
        style.configure("TSpinbox", fieldbackground="#252e3e", foreground=INK, padding=7, arrowsize=16)
        style.configure("TButton", background="#293448", foreground=INK, padding=(15, 9))
        style.map("TButton", background=[("active", "#35455e")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#082c25", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#82edd8")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK,
                        rowheight=44, borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#222b3b", foreground=MUTED, padding=(8, 12), font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#294b54")], foreground=[("selected", "#ecfff9")])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#2a3445", borderwidth=0, thickness=5)
        style.configure("Vertical.TScrollbar", background="#354055", troughcolor=PANEL, arrowsize=12)

    def build(self):
        menubar = tk.Menu(self.root)
        tasks_menu = tk.Menu(menubar, tearoff=False)
        tasks_menu.add_command(label="新建下载…", command=self.new)
        tasks_menu.add_command(label="批量导入链接…", command=self.import_links)
        tasks_menu.add_command(label="导出所有链接…", command=self.export_links)
        menubar.add_cascade(label="任务", menu=tasks_menu)
        self.root.configure(menu=menubar)
        side = tk.Frame(self.root, bg="#151b25", width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="FlowGet", font=("Segoe UI", 24, "bold"), bg="#151b25", fg=ACCENT).pack(anchor="w", padx=24, pady=(25, 0))
        tk.Label(side, text="下载，由你掌控", font=("Microsoft YaHei UI", 9), bg="#151b25", fg=MUTED).pack(anchor="w", padx=25, pady=(2, 34))
        self.nav = {}
        for title in ("全部任务", "正在下载", "已暂停", "已完成", "失败", "程序", "视频", "音乐", "文档", "压缩文件"):
            button = tk.Button(side, text=title, anchor="w", relief="flat", bd=0,
                font=("Microsoft YaHei UI", 10), padx=23, pady=10,
                bg="#151b25", fg=MUTED, activebackground="#253344", activeforeground=INK,
                command=lambda x=title: self.select_filter(x))
            button.pack(fill="x", padx=10, pady=1)
            self.nav[title] = button
        tk.Button(side, text="关于 / 实现进度", command=self.about, bg="#151b25", fg=MUTED,
                  relief="flat", font=("Microsoft YaHei UI", 9), pady=16).pack(side="bottom", fill="x")
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=26, pady=23)
        heading = tk.Frame(main, bg=BG)
        heading.pack(fill="x")
        self.heading = tk.Label(heading, text="全部任务", bg=BG, fg=INK, font=("Microsoft YaHei UI", 22, "bold"))
        self.heading.pack(side="left")
        self.query = tk.StringVar()
        self.search = ttk.Entry(heading, textvariable=self.query, width=25)
        self.search.pack(side="right", pady=5)
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
        for title, command, style in [
            ("＋ 新建下载", self.new, "Accent.TButton"), ("继续", self.resume, "TButton"),
            ("暂停", self.pause, "TButton"), ("队列", self.queue, "TButton"),
            ("保存目录", self.folder, "TButton"), ("设置", lambda: Settings(self), "TButton")]:
            ttk.Button(tools, text=title, command=command, style=style).pack(side="left", padx=(0, 7))
        self.queue_label = tk.Label(tools, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.queue_label.pack(side="right")
        table_box = tk.Frame(main, bg=PANEL)
        table_box.pack(fill="both", expand=True)
        columns = ("name", "size", "progress", "state", "speed", "eta")
        self.table = ttk.Treeview(table_box, columns=columns, show="headings", selectmode="extended", height=6)
        for c, title, width, stretch in zip(columns, ("文件名", "大小", "进度", "状态", "速度", "剩余时间"),
                (290, 95, 85, 115, 95, 95), (True, False, False, False, False, False)):
            self.table.heading(c, text=title)
            self.table.column(c, width=width, minwidth=60, stretch=stretch, anchor="w" if c == "name" else "center")
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_box, orient="vertical", command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.tag_configure("complete", foreground=ACCENT)
        self.table.tag_configure("error", foreground="#ff969d")
        self.table.bind("<<TreeviewSelect>>", lambda e: self.details())
        self.table.bind("<Double-1>", lambda e: self.details_popup())
        self.table.bind("<Button-3>", self.context)
        self.empty = tk.Label(table_box, text="准备好下一次下载\n\n点击「新建下载」，或按 Ctrl+V 粘贴链接", bg=PANEL, fg=MUTED,
                              font=("Microsoft YaHei UI", 12), justify="center")
        lower = tk.Frame(main, bg=PANEL, padx=16, pady=12)
        lower.pack(fill="x", pady=(12, 0))
        self.detail_name = tk.Label(lower, text="任务详情", bg=PANEL, fg=INK, font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.detail_name.pack(fill="x")
        self.progress = ttk.Progressbar(lower, maximum=100)
        self.progress.pack(fill="x", pady=(10, 8))
        self.detail_text = tk.Label(lower, text="选择任务，查看连接与校验状态", bg=PANEL, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left", wraplength=850)
        self.detail_text.pack(fill="x")
        self.segment_canvas = tk.Canvas(lower, height=16, bg=PANEL, highlightthickness=0)
        self.segment_canvas.pack(fill="x", pady=(8, 0))
        self.footer = tk.Label(main, text="", bg=BG, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
        self.footer.pack(fill="x", pady=(10, 0))

    def select_filter(self, value):
        self.filter = value
        self.heading.configure(text=value)
        self.render_table()

    def accept(self, t):
        group = self.filter
        if self.query.get().lower() not in (t["name"] + " " + t["url"]).lower():
            return False
        if group == "全部任务":
            return True
        if group == "正在下载":
            return t["status"] in ACTIVE or t["status"] == "queued"
        if group in ("已暂停", "已完成", "失败"):
            return t["status"] == {"已暂停": "paused", "已完成": "complete", "失败": "error"}[group]
        ext = Path(t["name"]).suffix.lower()
        return ext in {"程序": {".exe", ".msi", ".apk", ".msix"}, "视频": {".mp4", ".mkv", ".webm", ".avi", ".mov"},
            "音乐": {".mp3", ".flac", ".wav", ".m4a", ".ogg"}, "文档": {".pdf", ".docx", ".txt", ".xlsx", ".epub"},
            "压缩文件": {".zip", ".7z", ".rar", ".gz", ".tar"}}.get(group, set())

    def render_table(self):
        self.rows = {t["id"]: t for t in self.manager.snapshots()}
        wanted = [t for t in self.rows.values() if self.accept(t)]
        ids = {t["id"] for t in wanted}
        for old in self.table.get_children():
            if old not in ids:
                self.table.delete(old)
        for t in wanted:
            fraction = t["done"] / t["total"] if t["total"] else (1 if t["status"] == "complete" else 0)
            speed = t["speed"]
            remaining = max(0, t["total"] - t["done"]) / speed if t["total"] is not None and speed > 0 else None
            values = (t["name"], size(t["total"]), f"{fraction:.1%}", STATUS.get(t["status"], t["status"]),
                      size(speed) + "/s" if speed else "—", eta(remaining))
            if self.table.exists(t["id"]):
                self.table.item(t["id"], values=values, tags=(t["status"],))
            else:
                self.table.insert("", "end", iid=t["id"], values=values, tags=(t["status"],))
        if wanted:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.48, anchor="center")
        for title, button in self.nav.items():
            button.configure(bg="#263847" if title == self.filter else "#151b25", fg=ACCENT if title == self.filter else MUTED)

    def selected(self):
        return [self.rows[i] for i in self.table.selection() if i in self.rows]

    def details(self):
        tasks = self.selected()
        self.segment_canvas.delete("all")
        if not tasks:
            self.detail_name.configure(text="任务详情")
            self.detail_text.configure(text="选择任务，查看连接与校验状态")
            self.progress["value"] = 0
            return
        t = tasks[0]
        self.detail_name.configure(text=t["name"])
        fraction = t["done"] / t["total"] if t["total"] else (1 if t["status"] == "complete" else 0)
        self.progress["value"] = fraction * 100
        text = t["error"] or ("SHA-256  " + t["sha256"] if t["sha256"] else
            f"{size(t['done'])} / {size(t['total'])}   ·   {len(t['segments'])} 个分段   ·   " +
            ("支持校验续传" if t["resumable"] else "缺少强文件标识时使用单连接，重新开始可安全重下"))
        self.detail_text.configure(text=text, fg="#ff969d" if t["error"] else MUTED)
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
        self.metrics[0].configure(text=size(sum(t["speed"] for t in tasks)) + "/s")
        self.metrics[1].configure(text=f"{sum(t['status'] in ACTIVE for t in tasks):02}")
        self.metrics[2].configure(text=f"{sum(t['status'] == 'complete' for t in tasks):02}")
        scheduled = self.manager.settings.get("schedule_at")
        self.queue_label.configure(text="已定时" if scheduled else ("队列开启" if self.manager.settings["queue_enabled"] else "队列停止"))
        self.footer.configure(text="任务保存失败：" + self.manager.last_save_error if self.manager.last_save_error else
            f"{len(tasks)} 个任务    ·    FlowGet {VERSION}    ·    Ctrl+N 新建 / Ctrl+V 粘贴链接")
        self.details()
        if not self.closing:
            self.root.after(500, self.refresh)

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

    def pause(self):
        for t in self.selected():
            self.manager.pause(t["id"])

    def queue(self):
        enabled = not self.manager.settings["queue_enabled"]
        self.manager.configure(queue_enabled=enabled, schedule_at="")
        if not enabled:
            for t in self.manager.snapshots():
                if t["status"] in ACTIVE:
                    self.manager.pause(t["id"])

    def folder(self):
        tasks = self.selected()
        open_directory(tasks[0]["folder"] if tasks else self.manager.settings["folder"])

    def details_popup(self):
        tasks = self.selected()
        if not tasks:
            return
        t = tasks[0]
        dialog = Dialog(self, "任务属性", 700)
        text = tk.Text(dialog.body, bg="#252e3e", fg=INK, wrap="word", font=("Microsoft YaHei UI", 10), relief="flat", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", f"文件：{t['name']}\n状态：{STATUS[t['status']]}\n地址：{t['url']}\n目录：{t['folder']}\n已下载：{size(t['done'])}\n大小：{size(t['total'])}\nSHA-256：{t['sha256'] or '完成后计算'}\n预期 SHA-256：{t['expected_sha256'] or '未指定'}\n\n{t['error']}")
        text.configure(state="disabled")

    def context(self, event):
        item = self.table.identify_row(event.y)
        if item:
            self.table.selection_set(item)
        menu = tk.Menu(self.root, tearoff=False)
        for title, command in [("继续 / 重试", self.resume), ("暂停", self.pause), ("打开保存目录", self.folder),
                ("任务属性", self.details_popup), ("复制下载地址", self.copy_url), ("移除任务（保留文件）", self.remove)]:
            menu.add_command(label=title, command=command)
        menu.tk_popup(event.x_root, event.y_root)

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(t["url"] for t in self.selected()))

    def remove(self):
        if not messagebox.askyesno("移除任务", "从列表移除选中任务？已下载文件和临时分段都会保留。", parent=self.root):
            return
        try:
            for t in self.selected():
                self.manager.remove(t["id"])
        except ValueError as exc:
            messagebox.showinfo("请稍候", str(exc), parent=self.root)

    def about(self):
        messagebox.showinfo("FlowGet", f"FlowGet {VERSION}\n\n独立实现的 Windows 下载管理器。\n已实现：分段、续传、队列、计划、限速、校验、批量链接。\n已提供：浏览器主动发送链接扩展，需单独安装和联调。\n\n后续：自动接管、请求上下文、站点抓取、流媒体。\n本程序与 Tonec / IDM 无隶属关系。", parent=self.root)

    def close(self):
        self.closing = True
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
