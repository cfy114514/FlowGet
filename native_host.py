"""Chrome/Edge Native Messaging host: explicit links only, no cookies or HTTP API."""
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import uuid

from engine import atomic_json, check_url, clean_name

MAX_MESSAGE = 65536


def data_directory():
    return Path(os.environ.get("FLOWGET_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FlowGet")


def read_message(stream):
    header = stream.read(4)
    if len(header) != 4:
        raise ValueError("消息长度头不完整")
    length = struct.unpack("<I", header)[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError("消息过大或为空")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("消息内容不完整")
    return json.loads(body.decode("utf-8"))


def write_message(stream, value):
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def accept_message(origin, message, directory):
    directory = Path(directory)
    origin = origin.rstrip("/") + "/"
    config = json.loads((directory / "bridge.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"chrome-extension://[a-p]{32}/", origin) or origin not in config.get("allowed_origins", []):
        raise ValueError("扩展身份未获授权，请先运行注册脚本")
    if not isinstance(message, dict) or message.get("action") != "add":
        raise ValueError("不支持的消息")
    url = check_url(message.get("url", ""))
    name = message.get("name", "")
    if not isinstance(name, str) or len(name) > 500:
        raise ValueError("无效文件名")
    inbox = directory / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    if sum(1 for _ in inbox.glob("*.json")) >= 1000:
        raise ValueError("待处理任务已满，请打开 FlowGet")
    request_id = uuid.uuid4().hex
    atomic_json(inbox / f"{request_id}.json", {"url": url, "name": clean_name(name) if name else ""})
    return request_id


def ensure_desktop_running(directory):
    if sys.platform != "win32":
        return
    import msvcrt
    lock_path = Path(directory) / "instance.lock"
    if lock_path.exists():
        with lock_path.open("r+b") as lock:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    if getattr(sys, "frozen", False):
        app = Path(sys.executable).with_name("FlowGet.exe")
        if not app.is_file():
            raise ValueError("FlowGet.exe 必须与桥接程序放在同一目录")
        args = [str(app), "--from-browser"]
    else:
        app = Path(__file__).with_name("app.py")
        python = Path(sys.executable).with_name("pythonw.exe")
        args = [str(python), str(app), "--from-browser"]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)


def main():
    # Chrome supplies the caller origin on argv. Never accept an arbitrary local
    # command line invocation as an authenticated browser identity by default.
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        origin = sys.argv[1] if len(sys.argv) > 1 else ""
        directory = data_directory()
        request_id = accept_message(origin, read_message(sys.stdin.buffer), directory)
        ensure_desktop_running(directory)
        write_message(sys.stdout.buffer, {"ok": True, "id": request_id})
    except Exception as exc:
        write_message(sys.stdout.buffer, {"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()
