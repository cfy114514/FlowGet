"""Streaming adapter for the OS-supplied curl, using Windows TLS trust.

No shell is involved. Headers stay in a private temporary file; payload bytes
are consumed from stdout and validated by the engine before publication.
"""
from email.parser import Parser
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class CurlResponse:
    def __init__(self, executable, url, headers, use_proxy=True):
        self.directory = tempfile.TemporaryDirectory(prefix="flowget-http-")
        path = Path(self.directory.name) / "headers.txt"
        error_path = Path(self.directory.name) / "error.txt"
        self.error_file = error_path.open("w+b")
        self.process = None
        args = [executable, "--disable", "--silent", "--show-error", "--location", "--http1.1",
            "--max-redirs", "10", "--proto", "=http,https", "--proto-redir",
            "=https" if url.startswith("https:") else "=http,https",
            "--connect-timeout", "12", "--speed-time", "12", "--speed-limit", "1",
            "--dump-header", str(path), "--output", "-", "--globoff"]
        if ":" not in urllib.parse.urlsplit(url).hostname:
            args += ["--ipv4"]
        if not use_proxy:
            args += ["--noproxy", "*"]
        else:
            proxy = urllib.request.getproxies().get(urllib.parse.urlsplit(url).scheme)
            if proxy:
                args += ["--proxy", proxy]
        for key, value in headers.items():
            args += ["--header", f"{key}: {value}"]
        args += ["--url", url]
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=self.error_file,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                if path.exists():
                    raw = path.read_bytes().decode("iso-8859-1")
                    blocks = raw.replace("\r\n", "\n").split("\n\n")[:-1]
                    for block in reversed(blocks):
                        first, _, fields = block.partition("\n")
                        match = re.match(r"HTTP/\S+ (\d{3})(.*)", first)
                        if not match or "connection established" in first.lower():
                            continue
                        code = int(match[1])
                        if code < 200 or code in (301, 302, 303, 307, 308):
                            continue
                        self.status = code
                        self.headers = Parser().parsestr(fields)
                        if code >= 400:
                            raise urllib.error.HTTPError(url, code, match[2].strip(), self.headers, None)
                        return
                if self.process.poll() is not None:
                    self._raise_error("服务器未返回完整响应头")
                time.sleep(0.01)
            raise urllib.error.URLError("连接响应超时")
        except BaseException:
            self.close()
            raise

    def _raise_error(self, fallback):
        self.error_file.flush()
        self.error_file.seek(0)
        detail = self.error_file.read(2048).decode("utf-8", errors="replace").strip()
        raise urllib.error.URLError(detail or fallback)

    def read1(self, amount):
        block = self.process.stdout.read1(amount)
        if not block:
            code = self.process.wait(timeout=3)
            if code:
                self._raise_error(f"curl 返回错误 {code}")
        return block

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
        self.error_file.close()
        self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
