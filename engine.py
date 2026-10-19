"""Independent HTTP download engine; no IDM code, resources or file formats.

Segment payloads are the recovery journal. The JSON manifest fixes their ranges
and representation validator before any worker writes bytes.
"""
from __future__ import annotations

import concurrent.futures
import copy
from email.message import Message
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from transport import CurlResponse


ACTIVE = {"probing", "downloading", "verifying", "pausing"}


class Paused(Exception):
    pass


class ProtocolError(Exception):
    pass


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def clean_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "download.bin"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    # Keep room for segment and assembly suffixes on ordinary Windows paths.
    return name[:160]


def check_url(url: str) -> str:
    url = url.strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("请输入完整的 HTTP 或 HTTPS 下载地址")
    if parsed.username or parsed.password or any(ord(c) < 32 for c in url):
        raise ValueError("不支持在地址中嵌入用户名、密码或控制字符")
    # Support Unicode paths while preserving an already escaped signed URL.
    host = parsed.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port:
        host += ":" + str(parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host,
        urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(parsed.query, safe="%/?@:!$&'()*+,;=-._~"), ""))


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = check_url(newurl)
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
            raise ProtocolError("服务器尝试将 HTTPS 下载降级到 HTTP")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            new.remove_header("Cookie")
            new.remove_header("Authorization")
        return new


@dataclass
class Task:
    id: str
    url: str
    folder: str
    name: str
    connections: int = 8
    status: str = "queued"
    total: int | None = None
    done: int = 0
    speed: float = 0
    error: str = ""
    sha256: str = ""
    expected_sha256: str = ""
    created: float = field(default_factory=time.time)
    completed: float | None = None
    resumable: bool = False
    segments: list = field(default_factory=list)
    automatic_name: bool = False
    limit_kib: int = 0


class Manager:
    def __init__(self, data_dir: str | Path, *, autostart=True, use_proxy=True, backend="auto"):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.tasks: dict[str, Task] = {}
        self.running: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.stopping = False
        self.last_save_error = ""
        self.settings = {"connections": 8, "parallel": 2, "limit_kib": 0,
            "folder": str(Path.home() / "Downloads" / "FlowGet"), "queue_enabled": True,
            "schedule_at": "", "stop_at": ""}
        self.use_proxy = use_proxy
        # Use only the trusted OS copy, never an executable from the CWD/PATH.
        system_curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        self.curl = str(system_curl) if os.name == "nt" and system_curl.is_file() and backend != "urllib" else None
        if backend == "curl" and not self.curl:
            raise RuntimeError("系统 curl 不可用")
        self._limit_lock = threading.Lock()
        self._limit_next = time.monotonic()
        self._task_limit_next = {}
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.settings.update(state.get("settings", {}))
                fields = Task.__dataclass_fields__
                for item in state.get("tasks", []):
                    task = Task(**{k: v for k, v in item.items() if k in fields})
                    if task.status in ACTIVE:
                        task.status = "paused"
                    task.speed = 0
                    self.tasks[task.id] = task
            except (ValueError, TypeError, OSError) as exc:
                raise RuntimeError(f"任务文件读取失败；原文件已保留：{self.state_file}\n{exc}") from exc
        self.dispatcher = threading.Thread(target=self._dispatch, daemon=True)
        if autostart:
            self.dispatcher.start()

    def save(self):
        with self.save_lock:
            with self.lock:
                value = {"version": 1, "settings": copy.deepcopy(self.settings),
                    "tasks": [asdict(t) for t in self.tasks.values()]}
            atomic_json(self.state_file, value)

    def snapshots(self):
        with self.lock:
            return [asdict(t) for t in self.tasks.values()]

    def configure(self, **values):
        with self.lock:
            self.settings.update(values)
        self.save()

    def add(self, url, folder=None, name="", connections=None, expected_sha256="", start=True, request_id=None, limit_kib=0):
        url = check_url(url)
        expected_sha256 = expected_sha256.strip().lower()
        if expected_sha256 and not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise ValueError("SHA-256 应为 64 位十六进制字符")
        limit_kib = int(limit_kib)
        if not 0 <= limit_kib <= 1048576:
            raise ValueError("单任务限速应在 0–1048576 KB/s 之间")
        folder = str(Path(folder or self.settings["folder"]).resolve())
        automatic_name = not bool(name)
        name = clean_name(name or urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]))
        if request_id is not None and not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("无效任务标识")
        task = Task(request_id or uuid.uuid4().hex, url, folder, name,
            max(1, min(32, int(connections or self.settings["connections"]))),
            status="queued" if start else "paused", expected_sha256=expected_sha256,
            automatic_name=automatic_name, limit_kib=limit_kib)
        with self.lock:
            if task.id in self.tasks:
                return task.id
            self.tasks[task.id] = task
        self.save()
        return task.id

    def resume(self, task_id):
        with self.lock:
            t = self.tasks[task_id]
            if task_id in self.running or t.status == "complete":
                return
            t.status, t.error = "queued", ""
        self.save()

    def pause(self, task_id):
        with self.lock:
            t = self.tasks[task_id]
            if task_id in self.running:
                self.running[task_id][1].set()
                t.status = "pausing"
            elif t.status == "queued":
                t.status = "paused"
        self.save()

    def remove(self, task_id):
        with self.lock:
            if task_id in self.running:
                raise ValueError("请先暂停任务，等待所有连接停止后再移除")
            # Keep destination and partial payloads; never delete user files.
            self.tasks.pop(task_id, None)
            self._task_limit_next.pop(task_id, None)
        self.save()

    def update_task(self, task_id, *, url, name, connections, limit_kib=0):
        """Edit an idle task. A changed URL is re-probed with a fresh journal.

        URL identity is part of the existing recovery contract; never splice old
        payload into a replacement link without an independently proven identity.
        """
        url = check_url(url)
        connections, limit_kib = int(connections), int(limit_kib)
        if not 1 <= connections <= 32 or not 0 <= limit_kib <= 1048576:
            raise ValueError("连接数应为 1–32，限速应为 0–1048576 KB/s")
        with self.lock:
            t = self.tasks[task_id]
            if task_id in self.running or t.status in ACTIVE or t.status == "complete":
                raise ValueError("仅可编辑已停止的未完成任务，请先暂停并等待连接停止")
            if url != t.url:
                t.done, t.total, t.segments, t.resumable = 0, None, [], False
            t.url, t.name = url, clean_name(name or t.name)
            t.connections, t.limit_kib = connections, limit_kib
            t.automatic_name = False
            t.status, t.error = "paused", ""
            self._task_limit_next.pop(task_id, None)
        self.save()

    def move(self, task_ids, direction):
        """Move the selected block one position, preserving relative order."""
        if direction not in (-1, 1):
            raise ValueError("队列方向无效")
        selected = set(task_ids)
        with self.lock:
            order = list(self.tasks)
            indices = range(1, len(order)) if direction == -1 else range(len(order)-2, -1, -1)
            for i in indices:
                j = i + direction
                if order[i] in selected and order[j] not in selected:
                    order[i], order[j] = order[j], order[i]
            self.tasks = {key: self.tasks[key] for key in order}
        self.save()

    def set_queue(self, enabled, *, clear_schedule=True):
        with self.lock:
            self.settings["queue_enabled"] = bool(enabled)
            if clear_schedule:
                self.settings["schedule_at"] = ""
            if not enabled:
                for task_id, (_, stop) in self.running.items():
                    stop.set()
                    self.tasks[task_id].status = "pausing"
        self.save()

    def _dispatch(self):
        while not self.stopping:
            try:
                self._consume_inbox()
                with self.lock:
                    scheduled = self.settings.get("schedule_at", "")
                    if scheduled and time.time() >= float(scheduled):
                        self.settings["schedule_at"] = ""
                        self.settings["queue_enabled"] = True
                    stop_at = self.settings.get("stop_at", "")
                    if stop_at and time.time() >= float(stop_at):
                        self.settings["stop_at"] = ""
                        self.settings["schedule_at"] = ""
                        self.settings["queue_enabled"] = False
                        for task_id, (_, stop) in self.running.items():
                            stop.set()
                            self.tasks[task_id].status = "pausing"
                    slots = max(1, min(8, int(self.settings["parallel"]))) - len(self.running)
                    if self.settings["queue_enabled"]:
                        for t in self.tasks.values():
                            if slots <= 0:
                                break
                            if t.status == "queued" and t.id not in self.running:
                                stop = threading.Event()
                                worker = threading.Thread(target=self._run, args=(t, stop), daemon=True)
                                self.running[t.id] = (worker, stop)
                                t.status = "probing"
                                worker.start()
                                slots -= 1
                self.save()
            except Exception as exc:
                self.last_save_error = str(exc)
            time.sleep(0.35)

    def _consume_inbox(self):
        inbox = self.data_dir / "inbox"
        if not inbox.is_dir():
            return
        for path in list(inbox.glob("*.json"))[:20]:
            try:
                if not re.fullmatch(r"[a-f0-9]{32}", path.stem) or path.stat().st_size > 65536:
                    raise ValueError("无效浏览器任务")
                value = json.loads(path.read_text(encoding="utf-8"))
                self.add(value["url"], name=value.get("name", ""), request_id=path.stem)
                path.unlink()
            except Exception as exc:
                self.last_save_error = str(exc)
                try:
                    path.rename(path.with_suffix(".rejected"))
                except OSError:
                    pass

    def _opener(self):
        return urllib.request.build_opener(Redirects(), *([] if self.use_proxy else [urllib.request.ProxyHandler({})]))

    def _open(self, url, headers=None):
        all_headers = {"User-Agent": "FlowGet/0.2", "Accept-Encoding": "identity", **(headers or {})}
        if self.curl:
            return CurlResponse(self.curl, url, all_headers, self.use_proxy)
        request = urllib.request.Request(url, headers=all_headers)
        return self._opener().open(request, timeout=12)

    def _probe(self, t, stop):
        if stop.is_set():
            raise Paused()
        # GET rather than HEAD also works for servers rejecting HEAD.
        try:
            r = self._open(t.url, {"Range": "bytes=0-0"})
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and exc.headers.get("Content-Range") == "bytes */0":
                return {"size": 0, "validator": "", "validator_header": "", "range": False}
            raise
        with r:
            encoding = r.headers.get("Content-Encoding", "identity").lower()
            if encoding != "identity":
                raise ProtocolError("服务器忽略 identity 编码要求，无法安全分段")
            match = re.fullmatch(r"bytes 0-0/(\d+)", r.headers.get("Content-Range", ""))
            ranged = r.status == 206 and match is not None
            if r.status not in (200, 206) or (r.status == 206 and not ranged):
                raise ProtocolError("服务器返回了无效的探测响应")
            length = r.headers.get("Content-Length")
            size = int(match[1]) if ranged else (int(length) if length is not None else None)
            etag = r.headers.get("ETag", "")
            disposition = Message()
            disposition["Content-Disposition"] = r.headers.get("Content-Disposition", "")
            filename = disposition.get_filename() or ""
            # Require a strong ETag for cross-request byte assembly. Without one,
            # transfer once as a stream; Last-Modified alone may be too coarse.
            strong = etag if etag.startswith('"') and not etag.startswith('W/') else ""
            return {"size": size, "validator": strong,
                "validator_header": "ETag" if strong else "", "range": ranged,
                "filename": clean_name(filename) if filename else ""}

    def _parts(self, t):
        return Path(t.folder) / ".flowget-parts" / t.id

    def _prepare(self, t, info):
        directory = self._parts(t)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "manifest.json"
        old = None
        if manifest.exists():
            try:
                old = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        reuse = bool(old and info["range"] and info["validator"] and
            all(old.get(k) == info.get(k) for k in ("size", "validator", "validator_header")) and
            old.get("url") == t.url)
        if reuse:
            ranges = old["ranges"]
            # Defend against damaged journals rather than trusting arbitrary offsets.
            cursor = 0
            for lo, hi in ranges:
                if lo != cursor or hi < lo or hi >= info["size"]:
                    raise ProtocolError("分段记录损坏；为避免错误合并已停止")
                cursor = hi + 1
            if cursor != info["size"]:
                raise ProtocolError("分段记录不完整")
        else:
            for item in directory.glob("*.part"):
                item.unlink()
            size = info["size"]
            count = min(t.connections, max(1, math.ceil((size or 0) / (512 * 1024)))) if info["range"] and info["validator"] else 1
            ranges = []
            if size:
                chunk = math.ceil(size / count)
                ranges = [[start, min(start + chunk, size) - 1] for start in range(0, size, chunk)]
            elif size is None:
                ranges = [[0, None]]
            atomic_json(manifest, {**info, "url": t.url, "ranges": ranges})
        done = 0
        segments = []
        for i, (start, end) in enumerate(ranges):
            p = directory / f"{i}.part"
            n = p.stat().st_size if p.exists() else 0
            if end is not None and n > end - start + 1:
                raise ProtocolError("分段文件超出记录范围")
            done += n
            segments.append({"start": start, "end": end, "done": n})
        with self.lock:
            t.done, t.total, t.segments = done, info["size"], segments
            t.resumable = bool(info["range"] and info["validator"])
        return directory, ranges

    def _throttle(self, n, stop, task=None):
        while True:
            with self._limit_lock:
                now = time.monotonic()
                limit = int(self.settings.get("limit_kib", 0)) * 1024
                task_limit = task.limit_kib * 1024 if task else 0
                # Only consume the global budget when this task can send now;
                # a slow task must not reserve future slots ahead of other tasks.
                ready = now
                if limit > 0:
                    ready = max(ready, self._limit_next)
                if task_limit > 0:
                    ready = max(ready, self._task_limit_next.get(task.id, now))
                wait = ready - now
                if wait <= 0:
                    if limit > 0:
                        self._limit_next = now + n / limit
                    if task_limit > 0:
                        self._task_limit_next[task.id] = now + n / task_limit
                    return
            if stop.wait(min(wait, 0.1)):
                raise Paused()

    def _segment(self, t, stop, info, directory, index, bounds):
        lo, hi = bounds
        path = directory / f"{index}.part"
        last_error = None
        ranged = t.resumable
        for attempt in range(4):
            if stop.is_set():
                raise Paused()
            have = path.stat().st_size if path.exists() else 0
            expected = hi - lo + 1 if hi is not None else None
            if expected is not None and have == expected:
                return
            if not ranged and have:
                with self.lock:
                    t.done -= have
                    t.segments[index]["done"] = 0
                path.unlink()
                have = 0
            headers = {}
            if ranged:
                headers = {"Range": f"bytes={lo + have}-{hi}", "If-Range": info["validator"]}
            try:
                with self._open(t.url, headers) as response:
                    if stop.is_set():
                        raise Paused()
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ProtocolError("响应编码发生变化")
                    if ranged:
                        wanted = f"bytes {lo + have}-{hi}/{info['size']}"
                        if response.status != 206 or response.headers.get("Content-Range") != wanted:
                            raise ProtocolError("服务器拒绝范围请求或文件已变化，请重新开始此任务")
                        if response.headers.get("ETag") != info["validator"]:
                            raise ProtocolError("文件标识变化，已阻止不同版本的分段合并")
                    elif response.status != 200:
                        raise ProtocolError("单连接下载返回了异常状态")
                    remaining = expected - have if expected is not None else None
                    if response.headers.get("Content-Length") is not None and remaining is not None:
                        if int(response.headers["Content-Length"]) != remaining:
                            raise ProtocolError("响应长度与请求范围不符")
                    with path.open("ab", buffering=0) as f:
                        while True:
                            if stop.is_set():
                                raise Paused()
                            # read1 returns available bytes promptly for pause/speed UI.
                            block = response.read1(64 * 1024)
                            if not block:
                                break
                            if remaining is not None and len(block) > remaining:
                                raise ProtocolError("服务器发送了范围外的数据")
                            self._throttle(len(block), stop, t)
                            f.write(block)
                            with self.lock:
                                t.done += len(block)
                                t.segments[index]["done"] += len(block)
                            if remaining is not None:
                                remaining -= len(block)
                    if remaining not in (None, 0):
                        raise IOError("连接提前结束，正在重试")
                return
            except Paused:
                raise
            except ProtocolError:
                raise
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt < 3 and stop.wait(min(2 ** attempt, 4)):
                    raise Paused()
        raise IOError(f"重试 3 次后仍失败：{last_error}")

    def _assemble(self, t, stop, directory, ranges):
        with self.lock:
            t.status = "verifying"
        assembled = directory / "assembled.tmp"
        digest = hashlib.sha256()
        length = 0
        with assembled.open("wb") as output:
            for i, (lo, hi) in enumerate(ranges):
                with (directory / f"{i}.part").open("rb") as source:
                    while block := source.read(1024 * 1024):
                        if stop.is_set():
                            raise Paused()
                        output.write(block)
                        digest.update(block)
                        length += len(block)
            output.flush()
            os.fsync(output.fileno())
        if t.total is not None and length != t.total:
            raise ProtocolError("合并后的长度不匹配；未发布文件")
        actual = digest.hexdigest()
        if t.expected_sha256 and t.expected_sha256 != actual:
            raise ProtocolError(f"SHA-256 不匹配；未发布文件。实际：{actual}")
        name = Path(t.name)
        suffix = 0
        # Hard-link publication is atomic and never replaces an existing file.
        # Parts are in the destination volume. NTFS/ext4 support this operation.
        while True:
            if stop.is_set():
                raise Paused()
            target = Path(t.folder) / (t.name if suffix == 0 else f"{name.stem} ({suffix}){name.suffix}")
            try:
                os.link(assembled, target)
                break
            except FileExistsError:
                suffix += 1
            except OSError as exc:
                raise IOError("保存目录必须支持硬链接（如 NTFS）；临时文件已保留") from exc
        with self.lock:
            t.name, t.sha256, t.done, t.total = target.name, actual, length, length
            t.status, t.completed = "complete", time.time()
        # Save completion before deleting the recovery journal.
        self.save()
        shutil.rmtree(directory, ignore_errors=True)

    def _run(self, t, stop):
        sampler_stop = threading.Event()
        def sample():
            previous, when = t.done, time.monotonic()
            while not sampler_stop.wait(0.5):
                now = time.monotonic()
                with self.lock:
                    t.speed = max(0, t.done - previous) / max(0.01, now - when)
                    previous, when = t.done, now
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            info = self._probe(t, stop)
            if t.automatic_name and info.get("filename"):
                with self.lock:
                    t.name = info["filename"]
            directory, ranges = self._prepare(t, info)
            if stop.is_set():
                raise Paused()
            with self.lock:
                t.status, t.error = "downloading", ""
            if ranges:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as pool:
                    futures = [pool.submit(self._segment, t, stop, info, directory, i, bounds) for i, bounds in enumerate(ranges)]
                    first_error = None
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            if first_error is None:
                                first_error = exc
                            stop.set()
                    if first_error:
                        raise first_error
            if stop.is_set():
                raise Paused()
            self._assemble(t, stop, directory, ranges)
        except Paused:
            with self.lock:
                t.status = "paused"
        except Exception as exc:
            with self.lock:
                t.status, t.error = "error", str(exc)
        finally:
            sampler_stop.set()
            with self.lock:
                t.speed = 0
                self.running.pop(t.id, None)
                with self._limit_lock:
                    self._task_limit_next.pop(t.id, None)
            try:
                self.save()
            except OSError as exc:
                self.last_save_error = str(exc)

    def shutdown(self, timeout=20):
        self.stopping = True
        if self.dispatcher.is_alive():
            self.dispatcher.join(2)
        with self.lock:
            workers = list(self.running.values())
            for worker, stop in workers:
                stop.set()
        deadline = time.monotonic() + timeout
        for worker, stop in workers:
            worker.join(max(0, deadline - time.monotonic()))
        self.save()
        return all(not worker.is_alive() for worker, stop in workers)
