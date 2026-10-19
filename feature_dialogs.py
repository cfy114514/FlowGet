"""Queue management, FTP session login and asynchronous site discovery UI."""
from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from grabber import Crawler, DEFAULT_TYPES
from ui_base import Dialog


def queue_choices(manager):
    return {q["name"]: q["id"] for q in manager.queue_snapshots()}


class FTPLoginDialog(Dialog):
    def __init__(self, app, task):
        super().__init__(app, "FTP 登录 · 仅本次运行保留密码", 620)
        self.task_id = task["id"]
        self.label("用户名 · 留空使用 anonymous", 0)
        self.username = tk.StringVar(value=task.get("ftp_user", "anonymous"))
        ttk.Entry(self.body, textvariable=self.username).grid(row=1, column=0, sticky="ew")
        self.label("密码 · 不保存到任务文件", 2)
        self.password = tk.StringVar()
        ttk.Entry(self.body, textvariable=self.password, show="●").grid(row=3, column=0, sticky="ew")
        ttk.Label(self.body, text="FTP 使用明文传输。保存登录信息后，选择任务点击继续。", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=18)
        ttk.Button(self.body, text="保存本次登录", style="Accent.TButton", command=self.submit).grid(row=5, column=0, sticky="e")

    def submit(self):
        try:
            self.app.manager.set_ftp_login(self.task_id, self.username.get(), self.password.get())
            self.password.set("")
            self.destroy()
        except Exception as exc:
            messagebox.showerror("无法保存", str(exc), parent=self)


class QueueDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "管理下载队列", 850)
        self.geometry(f"850x650+{app.root.winfo_x()+60}+{app.root.winfo_y()+20}")
        self.body.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(self.body, columns=("name", "enabled", "parallel", "count"), show="headings", height=5)
        for key, title, width in (("name", "队列", 280), ("enabled", "状态", 150), ("parallel", "并发上限", 130), ("count", "任务数", 100)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="center")
        self.table.grid(row=0, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", lambda e: self.load())
        self.label("队列名称", 1)
        self.name = tk.StringVar()
        ttk.Entry(self.body, textvariable=self.name).grid(row=2, column=0, sticky="ew")
        self.label("队列并发上限 · 仍受全局并发上限约束", 3)
        self.parallel = tk.IntVar(value=2)
        ttk.Spinbox(self.body, from_=1, to=8, textvariable=self.parallel, width=10).grid(row=4, column=0, sticky="w")
        dates = ttk.Frame(self.body, style="Panel.TFrame")
        dates.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        dates.columnconfigure((0, 1), weight=1)
        self.start_at, self.stop_at = tk.StringVar(), tk.StringVar()
        for col, (label, variable) in enumerate((("定时启动 · 留空取消", self.start_at), ("定时停止 · 留空取消", self.stop_at))):
            ttk.Label(dates, text=label, style="Muted.TLabel").grid(row=0, column=col, sticky="w")
            ttk.Entry(dates, textvariable=variable).grid(row=1, column=col, sticky="ew", padx=(0, 8))
        ttk.Label(self.body, text="本机时间：YYYY-MM-DD HH:MM。全局队列需开启，队列内的暂停任务需手动继续。", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=12)
        buttons = ttk.Frame(self.body, style="Panel.TFrame")
        buttons.grid(row=7, column=0, sticky="ew")
        for title, command in (("新建队列", self.create), ("删除空队列", self.delete), ("启停队列", self.toggle),
                ("将主界面选中任务移入", self.assign), ("保存设置", self.submit)):
            ttk.Button(buttons, text=title, command=command, style="Compact.TButton").pack(side="left", padx=(0, 8))
        self.refresh(app.active_queue or "default")

    def selected_id(self):
        selected = self.table.selection()
        return selected[0] if selected else "default"

    def refresh(self, selected=None):
        selected = selected or self.selected_id()
        queues = self.app.manager.queue_snapshots()
        tasks = self.app.manager.snapshots()
        self.table.delete(*self.table.get_children())
        for q in queues:
            state = "等待定时启动" if q["schedule_at"] else "开启" if q["enabled"] else "停止"
            self.table.insert("", "end", iid=q["id"], values=(q["name"], state, q["parallel"], sum(t["queue_id"] == q["id"] for t in tasks)))
        self.table.selection_set(selected if self.table.exists(selected) else "default")
        self.load()
        self.app.refresh_selection()

    def load(self):
        q = next((q for q in self.app.manager.queue_snapshots() if q["id"] == self.selected_id()), None)
        if q:
            self.name.set(q["name"])
            self.parallel.set(q["parallel"])
            self.start_at.set(datetime.fromtimestamp(float(q["schedule_at"])).strftime("%Y-%m-%d %H:%M") if q["schedule_at"] else "")
            self.stop_at.set(datetime.fromtimestamp(float(q["stop_at"])).strftime("%Y-%m-%d %H:%M") if q["stop_at"] else "")

    def act(self, callback):
        try:
            callback()
        except Exception as exc:
            messagebox.showerror("队列操作未完成", str(exc), parent=self)

    def create(self):
        name = simpledialog.askstring("新建队列", "队列名称", parent=self)
        if name:
            self.act(lambda: self.refresh(self.app.manager.create_queue(name)))

    def delete(self):
        def apply():
            self.app.manager.delete_queue(self.selected_id())
            self.refresh("default")
        self.act(apply)

    def toggle(self):
        def apply():
            q = self.app.manager.queues[self.selected_id()]
            self.app.manager.set_queue(not q["enabled"], q["id"])
            self.refresh()
        self.act(apply)

    def assign(self):
        def apply():
            tasks = self.app.selected()
            if not tasks:
                raise ValueError("请先在主界面选中要移动的任务")
            queue_id = self.selected_id()
            self.app.manager.assign_queue([t["id"] for t in tasks], queue_id)
            self.app.active_queue = queue_id
            self.refresh(queue_id)
        self.act(apply)

    def submit(self):
        def apply():
            parse = lambda v: datetime.strptime(v.get().strip(), "%Y-%m-%d %H:%M").timestamp() if v.get().strip() else ""
            self.app.manager.configure_queue(self.selected_id(), name=self.name.get(), parallel=self.parallel.get(),
                schedule_at=parse(self.start_at), stop_at=parse(self.stop_at))
            self.refresh()
        self.act(apply)


class CrawlDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "站点抓取 · 发现并选择下载资源", 950)
        self.geometry(f"950x700+{app.root.winfo_x()+25}+{app.root.winfo_y()+10}")
        self.body.rowconfigure(6, weight=1)
        self.url = tk.StringVar()
        self.label("起始网页 · HTTP / HTTPS", 0)
        ttk.Entry(self.body, textvariable=self.url).grid(row=1, column=0, sticky="ew")
        options = ttk.Frame(self.body, style="Panel.TFrame")
        options.grid(row=2, column=0, sticky="ew", pady=12)
        self.depth, self.max_pages = tk.IntVar(value=2), tk.IntVar(value=50)
        self.same_directory = tk.BooleanVar(value=True)
        for label, value, upper in (("深度", self.depth, 10), ("最多页面", self.max_pages, 500)):
            ttk.Label(options, text=label).pack(side="left", padx=(0, 8))
            ttk.Spinbox(options, from_=0 if upper == 10 else 1, to=upper, textvariable=value, width=5).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(options, text="限起始目录及子目录", variable=self.same_directory).pack(side="left")
        self.label("文件类型 · 逗号分隔，如 .zip,.pdf；* 为所有类型", 3)
        self.extensions = tk.StringVar(value=DEFAULT_TYPES)
        ttk.Entry(self.body, textvariable=self.extensions).grid(row=4, column=0, sticky="ew")
        controls = ttk.Frame(self.body, style="Panel.TFrame")
        controls.grid(row=5, column=0, sticky="ew", pady=12)
        self.start_button = ttk.Button(controls, text="开始抓取", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.status = ttk.Label(controls, text="遵守 robots.txt · 同站点 · 不执行 JavaScript", style="Muted.TLabel")
        self.status.pack(side="left", padx=8)
        results = ttk.Frame(self.body, style="Panel.TFrame")
        results.grid(row=6, column=0, sticky="nsew")
        self.table = ttk.Treeview(results, columns=("name", "url", "source"), show="headings", selectmode="extended", height=5)
        self.table.heading("name", text="文件名")
        self.table.heading("url", text="资源地址 · Ctrl+A 全选，Ctrl/Shift 多选")
        self.table.heading("source", text="来源页面")
        self.table.column("name", width=170)
        self.table.column("url", width=440)
        self.table.column("source", width=250)
        scroll = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Control-a>", lambda e: self.select_all())
        self.notices = ttk.Label(self.body, text="最多发现 1000 个资源、读取 20 MB 网页。结果加入后为暂停任务，便于检查。", style="Muted.TLabel", wraplength=880)
        self.notices.grid(row=7, column=0, sticky="w", pady=10)
        footer = ttk.Frame(self.body, style="Panel.TFrame")
        footer.grid(row=8, column=0, sticky="ew")
        self.choices = queue_choices(app.manager)
        initial = next((name for name, key in self.choices.items() if key == app.active_queue), next(iter(self.choices)))
        self.destination = tk.StringVar(value=initial)
        ttk.Label(footer, text="目标队列").pack(side="left", padx=(0, 8))
        ttk.Combobox(footer, values=list(self.choices), textvariable=self.destination, state="readonly", width=16).pack(side="left")
        ttk.Button(footer, text="全选结果", command=self.select_all, style="Compact.TButton").pack(side="left", padx=8)
        self.import_button = ttk.Button(footer, text="加入选中资源", command=self.import_selected, style="Accent.TButton", state="disabled")
        self.import_button.pack(side="right")
        self.messages = queue.SimpleQueue()
        self.cancel = threading.Event()
        self.worker = None
        self.report = {"items": [], "errors": []}
        self.update_timer = self.after(100, self.poll)

    def select_all(self):
        self.table.selection_set(self.table.get_children())
        return "break"

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            crawler = Crawler(self.url.get(), depth=self.depth.get(), max_pages=self.max_pages.get(),
                extensions=self.extensions.get(), same_directory=self.same_directory.get(), use_proxy=self.app.manager.use_proxy)
        except Exception as exc:
            messagebox.showerror("无法抓取", str(exc), parent=self)
            return
        self.cancel.clear()
        self.table.delete(*self.table.get_children())
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.import_button.state(["disabled"])
        self.status.configure(text="正在读取 robots.txt…")
        # Keep the worker's closure free of Tk objects. A closed dialog may
        # otherwise be destroyed by the worker, invoking Tcl off the UI thread.
        messages, cancel = self.messages, self.cancel
        def work():
            try:
                report = crawler.run(cancel, lambda result: messages.put((False, result)))
            except Exception as exc:
                report = {"items": [], "pages": 0, "bytes": 0, "errors": [str(exc)], "cancelled": False, "truncated": False}
            messages.put((True, report))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel.set()
        self.status.configure(text="正在停止，等待当前连接返回…")

    def poll(self):
        try:
            while True:
                finished, report = self.messages.get_nowait()
                self.report = report
                for index, item in enumerate(report["items"]):
                    if not self.table.exists(str(index)):
                        self.table.insert("", "end", iid=str(index), values=(item["name"], item["url"], item["source"]))
                state = "已停止" if report["cancelled"] else "已达预算" if report["truncated"] else "已完成" if finished else "抓取中"
                self.status.configure(text=f"{state} · {report['pages']} 页 · {len(report['items'])} 个资源 · {len(report['errors'])} 条提示")
                if report["errors"]:
                    self.notices.configure(text=report["errors"][-1][:230])
                if finished:
                    self.start_button.state(["!disabled"])
                    self.stop_button.state(["disabled"])
                    self.import_button.state(["!disabled"] if report["items"] else ["disabled"])
        except queue.Empty:
            pass
        self.update_timer = self.after(100, self.poll)

    def import_selected(self):
        try:
            urls = [self.report["items"][int(key)]["url"] for key in self.table.selection()]
            if not urls:
                raise ValueError("请先选择要下载的资源")
            queue_id = self.choices[self.destination.get()]
            added = self.app.manager.add_batch(urls, queue_id=queue_id)
            self.app.active_queue = queue_id
            self.app.query.set("")
            self.app.select_filter("全部任务")
            self.status.configure(text=f"已添加 {len(added)} 个暂停任务；同队列重复链接已跳过")
            self.app.refresh_selection()
        except Exception as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)
